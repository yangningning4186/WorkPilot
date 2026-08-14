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
from app.core.run_bus import RunBus
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import build_model_gateway
from app.services.answer_stream import (
    AnswerDelta,
    AnswerFinished,
    AnswerProducer,
    produce_answer,
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
    producer: AnswerProducer = ctx.get("answer_producer") or produce_answer
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
                async for event in producer(
                    session,
                    gateway,
                    query=run.goal,
                    top_k=top_k,
                    settings=settings,
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
                "latency_ms": latency_ms,
                "cost_usd": await _run_cost_usd(session_factory, run_id=run_id),
            },
        )
        async with session_factory() as session:
            await finalize_message(
                session,
                message_id=message_id,
                status="completed",
                content=finished.answer,
                citations=[_citation_payload(citation) for citation in finished.citations],
            )
            await finish_run(session, run_id=run_id, status="done", worker_id=worker_id)
            await session.commit()
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
