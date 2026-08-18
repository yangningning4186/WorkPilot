"""定时维护: 回收失联 run, 落账过期费用预留。"""

from typing import Any

import structlog

from app.core.queue import get_run_queue
from app.core.run_bus import RunBus
from app.memory.store import list_dispatchable_memory_jobs
from app.services.cost_budget import sweep_expired_reservations
from app.services.runs import reap_expired_runs

logger = structlog.get_logger(__name__)


async def watchdog_tick(ctx: dict[str, Any]) -> int:
    """回收租约过期的 run: 可恢复的重新入队, 其余标记为失败。

    普通回答不自动重跑: 一次已经发出去的模型调用是否计费无法确认, 静默重放等于重复计费。
    自动恢复只给带 checkpoint 且工具满足幂等边界的固定综述 run(ADR-0007) ——
    它从最近 checkpoint 继续, 写回走 `tool_invocations` 幂等协议, 重放不会产生第二份笔记。
    """

    bus: RunBus = ctx["bus"]
    session_factory = ctx["session_factory"]
    settings = ctx["settings"]
    async with session_factory() as session:
        reaped = await reap_expired_runs(
            session, max_recovery=settings.run_max_recovery
        )
        await session.commit()
    queue = await get_run_queue()
    for run_id, attempt in reaped.recovered:
        # 先入队再唤醒: 客户端读到"正在恢复"时任务已经在队列里, 不会看到一段空窗。
        await queue.enqueue_review_run(run_id, attempt=attempt)
        await bus.publish(run_id)
    for run_id, attempt in reaped.recovered_cowork:
        await queue.enqueue_cowork_run(run_id, attempt=attempt)
        await bus.publish(run_id)
    for run_id in reaped.failed:
        # 唤醒还挂在 SSE 上的客户端, 让它们立刻读到 error 事件而不是干等心跳。
        await bus.publish(run_id)
    if reaped.recovered:
        logger.warning("重新入队失联的固定综述 run", count=len(reaped.recovered))
    if reaped.recovered_cowork:
        logger.warning("重新入队失联的 Cowork run", count=len(reaped.recovered_cowork))
    if reaped.failed:
        logger.warning("回收失联 run", count=len(reaped.failed))
    return len(reaped.failed) + len(reaped.recovered) + len(reaped.recovered_cowork)


async def cost_sweeper_tick(ctx: dict[str, Any]) -> int:
    """过期未结算的预留按上限落账, 否则额度会被永久占住。"""

    session_factory = ctx["session_factory"]
    async with session_factory() as session:
        swept = await sweep_expired_reservations(session)
    if swept:
        logger.warning("按上限结算过期费用预留", count=swept)
    return swept


async def memory_dispatch_tick(ctx: dict[str, Any]) -> int:
    """补偿 DB 作业已创建但首次 Redis 入队失败的窗口。"""

    settings = ctx["settings"]
    if not settings.memory_extraction_enabled:
        return 0
    session_factory = ctx["session_factory"]
    async with session_factory() as session:
        jobs = await list_dispatchable_memory_jobs(
            session, max_attempts=settings.memory_job_max_attempts
        )
        await session.commit()
    queue = ctx.get("run_queue") or await get_run_queue()
    for job_id, attempt in jobs:
        await queue.enqueue_memory_job(job_id, attempt=attempt)
    return len(jobs)
