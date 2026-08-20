"""问答 run 的执行体。

普通对话同样走 run(ADR-0007): 刷新恢复、断线续传、时间线渲染因此只有一套实现。
"""

import asyncio
import os
import socket
from dataclasses import dataclass, field
from decimal import Decimal
from time import monotonic
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.queue import get_run_queue
from app.core.run_bus import RunBus
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import build_model_gateway
from app.memory.recall import recall_memory_context
from app.memory.store import (
    MemoryExtractionJob,
    run_uses_owner_memory,
    schedule_memory_extraction,
)
from app.services.answer_stream import (
    AnswerDelta,
    AnswerFinished,
    AnswerProducer,
    produce_answer,
    produce_general_answer,
)
from app.services.conversation_context import (
    compact_conversation_context,
    load_conversation_context,
    resolve_contextual_query,
)
from app.services.cost_budget import BudgetExceededError
from app.services.model_budget import build_cost_guard
from app.services.runs import (
    append_message,
    claim_run,
    finalize_message,
    finish_run,
    get_run,
    renew_lease,
)
from app.worker.emitter import RunEventEmitter

logger = structlog.get_logger(__name__)


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass
class RunControl:
    """worker 自查的三个终止条件。"""

    cancelled: bool = False
    lease_lost: bool = False
    deadline: float = field(default=0.0)

    @property
    def wall_exceeded(self) -> bool:
        return self.deadline > 0 and monotonic() > self.deadline


class RunAborted(Exception):
    def __init__(
        self, *, status: str, user_message: str, code: str, retryable: bool, error: str
    ) -> None:
        super().__init__(error)
        self.status = status
        self.user_message = user_message
        self.code = code
        self.retryable = retryable
        self.error = error


async def answer_run(
    ctx: dict[str, Any],
    run_id_raw: str,
    top_k: int = 5,
) -> None:
    run_id = UUID(run_id_raw)
    settings: Settings = ctx.get("settings") or get_settings()
    bus: RunBus = ctx["bus"]
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    worker_id = worker_identity()

    async with session_factory() as session:
        run = await claim_run(
            session, run_id=run_id, worker_id=worker_id, lease_s=settings.run_lease_s
        )
        await session.commit()
    if run is None:
        # 队列重投递、已被别的 worker 领走、或创建后立刻取消。条件 UPDATE 抢不到就
        # 直接退出, 这是防双跑(以及重复计费)的主防线。
        logger.info("run 无法抢占, 跳过", run_id=str(run_id))
        return

    # 模式取自 run 记录而不是队列参数: 重投递或 watchdog 重跑时, 队列消息可能已经
    # 不在了, 但"这条回答是不是可溯源的"必须始终一致。
    default_producer: AnswerProducer = (
        produce_general_answer if run.answer_mode == "general" else produce_answer
    )
    producer: AnswerProducer = ctx.get("answer_producer") or default_producer

    emitter = RunEventEmitter(
        session_factory,
        bus,
        run_id=run_id,
        flush_interval_s=settings.run_delta_flush_ms / 1000,
        flush_chars=settings.run_delta_flush_chars,
    )
    control = RunControl(deadline=monotonic() + run.budget_wall_ms / 1000)

    async with session_factory() as session:
        message_id = await append_message(
            session,
            conversation_id=run.conversation_id,
            role="assistant",
            status="streaming",
            run_id=run_id,
        )
        await session.commit()
    await emitter.emit("message.start", {"message_id": str(message_id)})

    heartbeat = asyncio.create_task(
        _heartbeat_loop(
            session_factory,
            run_id=run_id,
            worker_id=worker_id,
            control=control,
            interval_s=settings.run_heartbeat_s,
            lease_s=settings.run_lease_s,
        )
    )
    started = monotonic()
    answer_text = ""
    try:
        async with session_factory() as session:
            gateway = build_model_gateway(
                settings,
                audit_sink=SqlLlmCallAudit(session),
                # 费用闸门用独立 session, 不随业务事务回滚。
                budget_guard=build_cost_guard(settings, session_factory),
                run_id=run_id,
            )
            try:
                finished: AnswerFinished | None = None
                conversation_context = ""
                retrieval_query = run.goal
                if settings.conversation_context_enabled:
                    answer_task_type = (
                        "general_answer" if run.answer_mode == "general" else "grounded_answer"
                    )
                    answer_max_tokens = (
                        settings.general_answer_max_tokens
                        if run.answer_mode == "general"
                        else settings.answer_max_tokens
                    )
                    answer_prompt_budget = gateway.prompt_budget(
                        answer_task_type,
                        max_tokens=answer_max_tokens,
                    )
                    conversation_token_limit = max(
                        200,
                        min(
                            answer_prompt_budget.max_input_tokens,
                            int(
                                answer_prompt_budget.context_window_tokens
                                * settings.conversation_summary_trigger_ratio
                            ),
                        ),
                    )
                    if settings.conversation_summary_enabled:
                        try:
                            compacted = await compact_conversation_context(
                                session,
                                gateway,
                                conversation_id=run.conversation_id,
                                current_run_id=run_id,
                                context_window_tokens=(answer_prompt_budget.context_window_tokens),
                                trigger_ratio=settings.conversation_summary_trigger_ratio,
                                keep_recent_turns=(settings.conversation_summary_keep_recent_turns),
                                max_summary_chars=settings.conversation_summary_max_chars,
                                max_input_chars=(settings.conversation_summary_input_max_chars),
                                max_tokens=settings.conversation_summary_max_tokens,
                            )
                            # 摘要是独立 checkpoint；后续记忆或检索降级时不应随业务
                            # session.rollback() 一起丢失。无触发时 commit 也是空操作。
                            await session.commit()
                            if compacted:
                                logger.info(
                                    "会话历史摘要已滚动更新",
                                    run_id=str(run_id),
                                    conversation_id=str(run.conversation_id),
                                )
                        except Exception:
                            await session.rollback()
                            logger.exception(
                                "会话历史摘要更新失败，回退原文上下文",
                                run_id=str(run_id),
                            )
                    try:
                        context = await load_conversation_context(
                            session,
                            conversation_id=run.conversation_id,
                            current_run_id=run_id,
                            max_turns=settings.conversation_context_max_turns,
                            max_chars=settings.conversation_context_max_chars,
                            max_input_tokens=conversation_token_limit,
                        )
                        conversation_context = context.text
                        if context.text:
                            try:
                                retrieval_query = await resolve_contextual_query(
                                    gateway,
                                    current_query=run.goal,
                                    context=context,
                                    max_tokens=settings.contextual_query_rewrite_max_tokens,
                                )
                            except Exception:
                                logger.exception(
                                    "多轮追问改写失败，回退当前问题",
                                    run_id=str(run_id),
                                )
                    except Exception:
                        await session.rollback()
                        logger.exception(
                            "会话上下文加载失败，降级为单轮回答",
                            run_id=str(run_id),
                        )
                memory_context = ""
                if settings.memory_recall_enabled and await run_uses_owner_memory(session, run_id):
                    try:
                        recalled = await recall_memory_context(
                            session,
                            gateway,
                            query=retrieval_query,
                            top_k=settings.memory_recall_top_k,
                            pinned_limit=settings.memory_pinned_limit,
                            max_chars=settings.memory_context_max_chars,
                        )
                        memory_context = recalled.text
                    except Exception:
                        # 记忆是增强项。召回服务/向量身份异常时回退到原问答，不能让
                        # 一条个人偏好把整条可靠 RAG 链路拖成失败。
                        await session.rollback()
                        logger.exception("长期记忆召回失败，降级为无记忆回答", run_id=str(run_id))
                async for event in producer(
                    session,
                    gateway,
                    query=run.goal,
                    top_k=top_k,
                    settings=settings,
                    memory_context=memory_context,
                    conversation_context=conversation_context,
                    retrieval_query=retrieval_query,
                ):
                    _check_control(control)
                    if isinstance(event, AnswerDelta):
                        answer_text += event.text
                        await emitter.delta(event.text)
                    else:
                        finished = event
                await session.commit()
            finally:
                await gateway.aclose()

        if finished is None:  # pragma: no cover - 生成器契约保证有终止事件
            raise RuntimeError("答案生成没有产出终止事件")

        for citation in finished.citations:
            await emitter.emit("citation", _citation_payload(citation))

        latency_ms = max(0, round((monotonic() - started) * 1000))
        await emitter.emit(
            "message.done",
            {
                "message_id": str(message_id),
                "refused": finished.refused,
                "refusal_reason": finished.refusal_reason,
                # 前端据此决定挂不挂"未经溯源"的免责标识; 缺省当作可溯源会更危险,
                # 所以这个字段是必填而不是可选。
                "grounded": finished.grounded,
                "latency_ms": latency_ms,
                "cost_usd": await _run_cost_usd(session_factory, run_id=run_id),
            },
        )
        memory_job: MemoryExtractionJob | None = None
        async with session_factory() as session:
            await finalize_message(
                session,
                message_id=message_id,
                status="completed",
                content=finished.answer,
                citations=[_citation_payload(citation) for citation in finished.citations],
            )
            finished_run = await finish_run(
                session, run_id=run_id, status="done", worker_id=worker_id
            )
            if finished_run and settings.memory_extraction_enabled:
                memory_job = await schedule_memory_extraction(session, run_id=run_id)
            await session.commit()
        if memory_job is not None:
            try:
                queue = ctx.get("run_queue") or await get_run_queue()
                await queue.enqueue_memory_job(memory_job.id, attempt=memory_job.attempts)
            except Exception:
                # 作业记录已经提交，dispatcher 会补偿；不能把已成功回答改成失败。
                logger.exception("长期记忆作业首次入队失败", job_id=str(memory_job.id))
    except RunAborted as aborted:
        await _abort(
            emitter,
            session_factory,
            run_id=run_id,
            worker_id=worker_id,
            message_id=message_id,
            content=answer_text,
            aborted=aborted,
        )
    except BudgetExceededError as error:
        await _abort(
            emitter,
            session_factory,
            run_id=run_id,
            worker_id=worker_id,
            message_id=message_id,
            content=answer_text,
            aborted=RunAborted(
                status="budget_exceeded",
                user_message="今日模型额度已用尽, 请明天再试。",
                code="daily_budget_exceeded",
                retryable=False,
                error=str(error),
            ),
        )
    except Exception as error:
        logger.exception("run 执行失败", run_id=str(run_id))
        await _abort(
            emitter,
            session_factory,
            run_id=run_id,
            worker_id=worker_id,
            message_id=message_id,
            content=answer_text,
            aborted=RunAborted(
                status="failed",
                user_message="回答生成失败, 请重试。",
                code="internal_error",
                retryable=True,
                error=str(error),
            ),
        )
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass


def _check_control(control: RunControl) -> None:
    if control.cancelled:
        raise RunAborted(
            status="cancelled",
            user_message="本次回答已取消。",
            code="cancelled",
            retryable=True,
            error="用户取消",
        )
    if control.lease_lost:
        # 租约被 watchdog 或别的 worker 接管, 继续写事件会破坏 seq 单调, 立即让位。
        raise RunAborted(
            status="failed",
            user_message="回答意外中断, 请重新提问。",
            code="lease_lost",
            retryable=True,
            error="租约丢失",
        )
    if control.wall_exceeded:
        raise RunAborted(
            status="failed",
            user_message="回答超时, 请重试或缩小问题范围。",
            code="wall_clock_exceeded",
            retryable=True,
            error="超出 run 墙钟预算",
        )


async def _heartbeat_loop(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: UUID,
    worker_id: str,
    control: RunControl,
    interval_s: float,
    lease_s: int,
) -> None:
    """续租的同时把取消请求带回来, 省掉一次单独查询。"""

    while True:
        await asyncio.sleep(interval_s)
        async with session_factory() as session:
            record = await renew_lease(session, run_id=run_id, worker_id=worker_id, lease_s=lease_s)
            await session.commit()
        if record is None:
            control.lease_lost = True
            return
        if record.cancel_requested:
            control.cancelled = True
            return


async def _abort(
    emitter: RunEventEmitter,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: UUID,
    worker_id: str,
    message_id: UUID,
    content: str,
    aborted: RunAborted,
) -> None:
    """失败路径同样要落事件与消息终态, 不能静默消失。"""

    await emitter.emit(
        "error",
        {
            "user_message": aborted.user_message,
            "retryable": aborted.retryable,
            "code": aborted.code,
        },
    )
    message_status = "cancelled" if aborted.status == "cancelled" else "failed"
    async with session_factory() as session:
        await finalize_message(
            session, message_id=message_id, status=message_status, content=content
        )
        # 租约已经不在手上时不带 worker_id, 否则条件不满足会留下永不收尾的 run。
        await finish_run(
            session,
            run_id=run_id,
            status=aborted.status,
            worker_id=None if aborted.code == "lease_lost" else worker_id,
            error=aborted.error,
        )
        await session.commit()


async def _run_cost_usd(session_factory: async_sessionmaker[AsyncSession], *, run_id: UUID) -> str:
    """本次 run 的累计费用, 让成本对用户可见(docs/08 §3.2)。"""

    async with session_factory() as session:
        total = (
            await session.execute(
                text("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
        ).scalar_one()
    return str(Decimal(total))


def _citation_payload(citation: Any) -> dict[str, Any]:
    """引用必须带完整定位元数据(约束 3), 只给 bbox 四个数换渲染器就会错位。"""

    return {
        "citation_id": citation.citation_id,
        "block_id": str(citation.block_id),
        "version_id": str(citation.version_id),
        "doc_id": str(citation.document_id),
        "title": citation.title,
        "source_uri": citation.source_uri,
        "quote": citation.quote,
        "char_start": citation.char_start,
        "char_end": citation.char_end,
        "heading_path": list(citation.heading_path),
        "locations": list(citation.locations),
    }


async def check_run_exists(session: AsyncSession, run_id: UUID) -> bool:
    return await get_run(session, run_id) is not None
