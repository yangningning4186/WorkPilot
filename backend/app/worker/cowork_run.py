"""Cowork 动态工具循环的 worker 执行体。"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.budget import BudgetedGateway, BudgetMeter
from app.agent.cowork_runtime import run_cowork_graph
from app.agent.cowork_tools import CoworkToolRegistry, build_default_cowork_registry
from app.agent.state import BudgetState
from app.core.config import Settings, get_settings
from app.core.run_bus import RunBus
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import ModelGateway, build_model_gateway
from app.services.model_budget import build_cost_guard
from app.services.runs import (
    append_events,
    append_message,
    claim_run,
    finalize_message,
    finish_run,
    get_run,
    renew_lease,
)
from app.worker.answer_run import worker_identity

logger = structlog.get_logger(__name__)


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

    async with session_factory() as session:
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
        async with session_factory() as session:
            if raw_gateway is None:
                raw_gateway = build_model_gateway(
                    settings,
                    audit_sink=SqlLlmCallAudit(session),
                    budget_guard=build_cost_guard(settings, session_factory),
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
            registry: CoworkToolRegistry = (
                ctx.get("cowork_registry") or build_default_cowork_registry()
            )
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
            await finish_run(
                session,
                run_id=run_id,
                status=run_status,
                worker_id=worker_id,
                error=state["error"],
            )
            await session.commit()
        await bus.publish(run_id)
    except Exception as error:
        logger.exception("Cowork run 执行失败", run_id=str(run_id))
        await _fail_cowork(
            session_factory,
            bus,
            run_id=run_id,
            worker_id=worker_id,
            message_id=message_id,
            error=str(error),
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
        return "Cowork 执行失败，未返回具体原因。请重试或缩小任务范围。"
    if len(detail) > 360:
        detail = f"{detail[:357]}…"
    return f"Cowork 执行失败：{detail}"
