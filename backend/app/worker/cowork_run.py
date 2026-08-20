"""Cowork 动态工具循环的 worker 执行体。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_core.budget import BudgetedGateway, BudgetMeter
from app.agent_core.contracts import BudgetState
from app.core.config import Settings, get_settings
from app.core.queue import get_run_queue
from app.core.run_bus import RunBus
from app.cowork.automation_tools import register_scheduler_tools
from app.cowork.browser_tools import PlaywrightBrowserManager, register_browser_tools
from app.cowork.connector_tools import register_connector_tools
from app.cowork.extensions import register_mcp_tools, register_skill_tools
from app.cowork.mcp.client import McpClientManager
from app.cowork.mcp.config import McpConfiguration, load_mcp_configuration
from app.cowork.mcp.credentials import hydrate_mcp_oauth_credentials
from app.cowork.provider_profiles import build_conversation_gateway
from app.cowork.rag_tools import register_rag_tools
from app.cowork.runtime import run_cowork_graph
from app.cowork.skills.distillation_store import schedule_skill_distillation, successful_tool_names
from app.cowork.subagent import register_readonly_subagent
from app.cowork.tools import CoworkToolRegistry, build_default_cowork_registry
from app.cowork_store.factory import local_cowork_stores
from app.cowork_store.routing import configured_cowork_store
from app.rag.memory.store import schedule_memory_extraction
from app.rag.service import PostgresRagService
from app.runstore.runs import (
    append_events,
    append_message,
    claim_run,
    finalize_message,
    finish_run,
    get_run,
    renew_lease,
)
from app.security.secret_store import LocalSecretStore
from app.worker.answer_run import worker_identity
from workpilot_ai.gateway import ModelGateway

logger = structlog.get_logger(__name__)


def _mcp_configuration_sha256(configuration: McpConfiguration) -> str:
    payload = json.dumps(
        configuration.model_dump(mode="json", exclude={"source_path"}),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


async def _cached_mcp_manager(
    ctx: dict[str, Any],
    configuration: McpConfiguration,
    settings: Settings,
) -> McpClientManager:
    raw_lock = ctx.get("mcp_manager_lock")
    if raw_lock is None:
        raw_lock = asyncio.Lock()
        ctx["mcp_manager_lock"] = raw_lock
    if not isinstance(raw_lock, asyncio.Lock):
        raise TypeError("mcp_manager_lock 类型无效")
    fingerprint = _mcp_configuration_sha256(configuration)
    async with raw_lock:
        raw_cache = ctx.setdefault("mcp_manager_cache", {})
        if not isinstance(raw_cache, dict):
            raise TypeError("mcp_manager_cache 类型无效")
        cache = cast("dict[str, McpClientManager]", raw_cache)
        manager = cache.get(fingerprint)
        if manager is None:
            manager = McpClientManager(
                configuration,
                connect_timeout_s=settings.cowork_mcp_connect_timeout_s,
                call_timeout_s=settings.cowork_mcp_call_timeout_s,
                result_max_chars=settings.cowork_mcp_result_max_chars,
            )
            cache[fingerprint] = manager
        return manager


async def cowork_run(ctx: dict[str, Any], run_id_raw: str) -> None:
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
        logger.info("Cowork run 无法抢占，跳过", run_id=str(run_id))
        return
    if run.workflow_type != "cowork":
        await _fail_cowork(
            session_factory,
            bus,
            run_id=run_id,
            worker_id=worker_id,
            message_id=None,
            error="队列任务与 workflow_type 不匹配",
        )
        return

    local_user_message = None
    async with session_factory() as session:
        store = configured_cowork_store()
        if store is not None and await store.get_run(run_id) is not None:
            conversation_messages = await local_cowork_stores().conversations.read(
                run.conversation_id
            )
            existing_message = next(
                (
                    item
                    for item in conversation_messages
                    if item.run_id == run_id and item.role == "assistant"
                ),
                None,
            )
            local_user_message = next(
                (
                    item
                    for item in reversed(conversation_messages)
                    if item.run_id == run_id and item.role == "user"
                ),
                None,
            )
            existing = None if existing_message is None else existing_message.record_id
        else:
            existing = (
                await session.execute(
                    text(
                        """
                        SELECT id FROM messages
                        WHERE run_id = :run_id AND role = 'assistant'
                        ORDER BY seq LIMIT 1
                        """
                    ),
                    {"run_id": run_id},
                )
            ).scalar_one_or_none()
        message_id = (
            UUID(str(existing))
            if existing is not None
            else await append_message(
                session,
                conversation_id=run.conversation_id,
                role="assistant",
                status="streaming",
                run_id=run_id,
            )
        )
        if existing is None:
            await append_events(
                session,
                run_id=run_id,
                events=[("message.start", {"message_id": str(message_id)})],
            )
        await session.commit()
    if existing is None:
        await bus.publish(run_id)

    cancel_event = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat(
            session_factory,
            run_id=run_id,
            worker_id=worker_id,
            interval_s=settings.run_heartbeat_s,
            lease_s=settings.run_lease_s,
            cancel_event=cancel_event,
        )
    )
    cancel_watcher = asyncio.create_task(
        _watch_cancel(
            session_factory,
            bus,
            run_id=run_id,
            poll_s=settings.cowork_cancel_poll_s,
            cancel_event=cancel_event,
        )
    )
    raw_gateway: ModelGateway | None = ctx.get("cowork_gateway")
    owns_gateway = raw_gateway is None
    try:
        memory_job = None
        skill_job = None
        async with session_factory() as session:
            if raw_gateway is None:
                raw_gateway = await build_conversation_gateway(
                    session,
                    conversation_id=run.conversation_id,
                    settings=settings,
                    session_factory=session_factory,
                    run_id=run_id,
                )
            budget: BudgetState = {
                "max_tokens": run.budget_tokens,
                "used_tokens": run.used_tokens,
                "max_calls": run.budget_calls,
                "used_calls": run.used_calls,
                "max_wall_ms": run.budget_wall_ms,
                "used_wall_ms": 0,
                "started_at_ms": 0,
            }
            meter = BudgetMeter(budget, chars_per_token=settings.cost_estimate_chars_per_token)
            configured_registry = ctx.get("cowork_registry")
            if configured_registry is None:
                registry = build_default_cowork_registry()
                register_skill_tools(registry, settings)
                manager = ctx.get("mcp_manager")
                if not isinstance(manager, McpClientManager):
                    configuration = await hydrate_mcp_oauth_credentials(
                        session,
                        load_mcp_configuration(settings.cowork_mcp_config_path),
                        LocalSecretStore(settings.secret_store_key_path),
                    )
                    manager = await _cached_mcp_manager(ctx, configuration, settings)
                await register_mcp_tools(registry, manager)
                browser_manager = ctx.get("browser_manager")
                if not isinstance(browser_manager, PlaywrightBrowserManager):
                    browser_manager = PlaywrightBrowserManager(
                        idle_ttl_s=settings.cowork_browser_session_idle_ttl_s,
                        max_ttl_s=settings.cowork_browser_session_max_ttl_s,
                    )
                    ctx["browser_manager"] = browser_manager
                register_browser_tools(registry, browser_manager)
                register_connector_tools(registry)
                register_scheduler_tools(registry)
                register_readonly_subagent(registry)
                register_rag_tools(
                    registry,
                    PostgresRagService(
                        session_factory,
                        lexical_enabled=settings.lexical_rrf_enabled,
                        lexical_mode=settings.lexical_mode,
                        rrf_k=settings.rrf_k,
                        settings=settings,
                    ),
                )
            else:
                registry = configured_registry
            assert isinstance(registry, CoworkToolRegistry)
            state = await run_cowork_graph(
                session,
                run_id=run_id,
                registry=registry,
                gateway=BudgetedGateway(raw_gateway, meter),
                meter=meter,
                settings=settings,
                worker_id=worker_id,
                bus=bus,
                cancel_event=cancel_event,
                session_factory=session_factory,
            )

        if state["status"] == "waiting_human":
            # runtime 已把 checkpoint、interrupt 事件和 run.waiting_human 原子提交。
            # 保留同一条 streaming assistant message，答复后由新的队列作业继续写完。
            logger.info("Cowork 等待用户处理运行中请求", run_id=str(run_id))
            return

        terminal_status = state["status"]
        final_text = state["final_message"] or "Cowork 任务已结束。"
        run_status = (
            "done"
            if terminal_status == "done"
            else "cancelled"
            if terminal_status == "cancelled"
            else "budget_exceeded"
            if terminal_status == "budget_exceeded"
            else "failed"
        )
        message_status = (
            "completed"
            if run_status == "done"
            else "cancelled"
            if run_status == "cancelled"
            else "failed"
        )
        async with session_factory() as session:
            await finalize_message(
                session,
                message_id=message_id,
                status=message_status,
                content=final_text,
            )
            events: list[tuple[str, dict[str, Any]]] = [
                ("message.delta", {"text": final_text}),
                (
                    "message.done",
                    {"message_id": str(message_id), "status": message_status},
                ),
                (
                    "run.done",
                    {"workflow_type": "cowork", "status": run_status},
                ),
            ]
            await append_events(session, run_id=run_id, events=events)
            finished_run = await finish_run(
                session,
                run_id=run_id,
                status=run_status,
                worker_id=worker_id,
                error=state["error"],
            )
            if (
                finished_run
                and run_status == "done"
                and settings.cowork_store_backend != "sqlite"
            ):
                if settings.memory_extraction_enabled:
                    memory_job = await schedule_memory_extraction(session, run_id=run_id)
                if settings.skill_distillation_enabled:
                    skill_job = await schedule_skill_distillation(session, run_id=run_id)
            await session.commit()
        await bus.publish(run_id)
        if (
            finished_run
            and run_status == "done"
            and settings.cowork_store_backend == "sqlite"
        ):
            try:
                async with session_factory() as post_session:
                    if settings.memory_extraction_enabled:
                        if local_user_message is None:
                            raise LookupError("SQLite Cowork run 缺少用户来源消息")
                        memory_job = await schedule_memory_extraction(
                            post_session,
                            run_id=run_id,
                            local_source_message_id=local_user_message.record_id,
                            local_conversation_id=run.conversation_id,
                            local_content=local_user_message.content,
                            local_created_at=datetime.fromisoformat(local_user_message.created_at),
                        )
                    if settings.skill_distillation_enabled:
                        skill_job = await schedule_skill_distillation(
                            post_session,
                            run_id=run_id,
                            local_goal=run.goal,
                            local_final_message=final_text,
                            local_successful_tools=successful_tool_names(
                                cast("dict[str, Any]", state)
                            ),
                        )
                    await post_session.commit()
            except Exception:
                # Cowork 主运行已经持久化为成功；后处理失败只能告警，不能反向改终态。
                logger.exception("SQLite Cowork 后处理作业创建失败", run_id=str(run_id))
        if memory_job is not None or skill_job is not None:
            try:
                queue = ctx.get("run_queue") or await get_run_queue()
                if memory_job is not None:
                    await queue.enqueue_memory_job(memory_job.id, attempt=memory_job.attempts)
                if skill_job is not None:
                    await queue.enqueue_skill_job(skill_job.id, attempt=skill_job.attempts)
            except Exception:
                # 作业已可靠落库，定时 dispatcher 会补偿；不能把已成功的 Cowork 改成失败。
                logger.exception("Cowork 后处理作业首次入队失败", run_id=str(run_id))
    except Exception as error:
        error_detail = _cowork_error_detail(error)
        logger.exception(
            "Cowork run 执行失败",
            run_id=str(run_id),
            exception_type=type(error).__name__,
            exception_detail=error_detail,
        )
        await _fail_cowork(
            session_factory,
            bus,
            run_id=run_id,
            worker_id=worker_id,
            message_id=message_id,
            error=error_detail,
        )
    finally:
        if owns_gateway and raw_gateway is not None:
            await raw_gateway.aclose()
        heartbeat.cancel()
        cancel_watcher.cancel()
        try:
            await asyncio.gather(heartbeat, cancel_watcher)
        except asyncio.CancelledError:
            pass


async def _watch_cancel(
    session_factory: async_sessionmaker[AsyncSession],
    bus: RunBus,
    *,
    run_id: UUID,
    poll_s: float,
    cancel_event: asyncio.Event,
) -> None:
    """事件唤醒优先、短轮询兜底，把跨进程 cancel 迅速送进可终止工具。"""

    async with bus.subscribe(run_id) as subscription:
        while not cancel_event.is_set():
            async with session_factory() as session:
                run = await get_run(session, run_id)
            if run is None or run.cancel_requested or run.is_terminal:
                cancel_event.set()
                return
            await subscription.wait(poll_s)


async def _heartbeat(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: UUID,
    worker_id: str,
    interval_s: float,
    lease_s: int,
    cancel_event: asyncio.Event,
) -> None:
    while True:
        await asyncio.sleep(interval_s)
        async with session_factory() as session:
            renewed = await renew_lease(
                session,
                run_id=run_id,
                worker_id=worker_id,
                lease_s=lease_s,
            )
            await session.commit()
        if renewed is None:
            return
        if renewed.cancel_requested:
            # 继续续租直到当前不可中断的模型/文件步骤抵达安全边界，避免取消过程中
            # lease 过期后 watchdog 又把同一个副作用任务交给第二个 worker。
            cancel_event.set()


async def _fail_cowork(
    session_factory: async_sessionmaker[AsyncSession],
    bus: RunBus,
    *,
    run_id: UUID,
    worker_id: str,
    message_id: UUID | None,
    error: str,
) -> None:
    user_message = _cowork_failure_message(error)
    async with session_factory() as session:
        if message_id is not None:
            await finalize_message(
                session,
                message_id=message_id,
                status="failed",
                content=user_message,
            )
        await append_events(
            session,
            run_id=run_id,
            events=[
                (
                    "error",
                    {
                        "code": "cowork_failed",
                        "retryable": True,
                        "user_message": user_message,
                    },
                )
            ],
        )
        await finish_run(
            session,
            run_id=run_id,
            status="failed",
            worker_id=worker_id,
            error=error,
        )
        await session.commit()
    await bus.publish(run_id)


def _cowork_failure_message(error: str) -> str:
    """Owner 对话里给出可行动的短错误，同时限制异常文本体积。"""

    detail = " ".join(error.split())
    if not detail:
        return "Cowork 执行失败：运行时异常未携带说明，错误类型已记录，请重试。"
    if len(detail) > 360:
        detail = f"{detail[:357]}…"
    return f"Cowork 执行失败：{detail}"


def _cowork_error_detail(error: BaseException) -> str:
    """保证数据库和客户端永远拿到非空、可诊断的异常说明。"""

    detail = " ".join(str(error).split())
    if detail:
        return detail
    if isinstance(error, TimeoutError):
        return "模型或工具请求超时，请重试"
    return "运行时异常未携带说明；错误类型已写入日志，请重试"
