"""固定综述 run 的独立 worker 执行体。"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.review_graph import run_readonly_review
from app.agent.review_tools import DatabaseModelReviewTools
from app.core.config import Settings, get_settings
from app.core.run_bus import RunBus
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import build_model_gateway
from app.services.model_budget import build_cost_guard
from app.services.runs import append_events, claim_run, finish_run, renew_lease
from app.worker.answer_run import worker_identity

logger = structlog.get_logger(__name__)


async def review_run(ctx: dict[str, Any], run_id_raw: str) -> None:
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
        logger.info("固定综述 run 无法抢占，跳过", run_id=str(run_id))
        return
    if run.workflow_type != "literature_review":
        await _fail_review(
            session_factory,
            bus,
            run_id=run_id,
            worker_id=worker_id,
            error="队列任务与 workflow_type 不匹配",
        )
        return

    heartbeat = asyncio.create_task(
        _heartbeat(
            session_factory,
            run_id=run_id,
            worker_id=worker_id,
            interval_s=settings.run_heartbeat_s,
            lease_s=settings.run_lease_s,
        )
    )
    try:
        # 每次失败都会先落 checkpoint；重试只从失败节点开始，不重跑已完成步骤。
        for attempt in range(3):
            try:
                async with session_factory() as session:
                    gateway = build_model_gateway(
                        settings,
                        audit_sink=SqlLlmCallAudit(session),
                        budget_guard=build_cost_guard(settings, session_factory),
                        run_id=run_id,
                    )
                    try:
                        tools = DatabaseModelReviewTools(session, gateway)
                        await run_readonly_review(
                            session, run_id=run_id, tools=tools, bus=bus
                        )
                    finally:
                        await gateway.aclose()
                return
            except Exception:
                if attempt < 2:
                    logger.warning(
                        "固定综述步骤失败，将从 checkpoint 重试",
                        run_id=str(run_id),
                        attempt=attempt + 1,
                    )
                    continue
                raise
    except Exception as error:
        logger.exception("固定综述 run 执行失败", run_id=str(run_id))
        await _fail_review(
            session_factory,
            bus,
            run_id=run_id,
            worker_id=worker_id,
            error=str(error),
        )
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass


async def _heartbeat(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: UUID,
    worker_id: str,
    interval_s: float,
    lease_s: int,
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


async def _fail_review(
    session_factory: async_sessionmaker[AsyncSession],
    bus: RunBus,
    *,
    run_id: UUID,
    worker_id: str,
    error: str,
) -> None:
    async with session_factory() as session:
        await append_events(
            session,
            run_id=run_id,
            events=[
                (
                    "error",
                    {
                        "user_message": "固定综述执行失败，可从最近 checkpoint 重试。",
                        "retryable": True,
                        "code": "review_failed",
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
