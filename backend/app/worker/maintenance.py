"""定时维护: 回收失联 run, 落账过期费用预留。"""

import asyncio
from typing import Any

import structlog

from app.core.queue import get_run_queue
from app.core.run_bus import RunBus
from app.cowork.memory import list_dispatchable_memory_jobs
from app.cowork.schedules import (
    claim_due_sleeping_runs,
    dispatch_due_schedules,
    list_dispatchable_scheduled_runs,
)
from app.cowork.skills.candidate_store import list_dispatchable_skill_jobs
from app.runstore.runs import reap_expired_runs
from app.telemetry import default_telemetry_store

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
        reaped = await reap_expired_runs(session, max_recovery=settings.run_max_recovery)
        await session.commit()
    queue = await get_run_queue()
    for run_id, attempt in reaped.recovered_cowork:
        # 先入队再唤醒: 客户端读到"正在恢复"时任务已经在队列里, 不会看到一段空窗。
        await queue.enqueue_cowork_run(run_id, attempt=attempt)
        await bus.publish(run_id)
    for run_id in reaped.failed:
        # 唤醒还挂在 SSE 上的客户端, 让它们立刻读到 error 事件而不是干等心跳。
        await bus.publish(run_id)
    for run_id in reaped.cancelled:
        await bus.publish(run_id)
    if reaped.recovered_cowork:
        logger.warning("重新入队失联的 Cowork run", count=len(reaped.recovered_cowork))
    if reaped.failed:
        logger.warning("回收失联 run", count=len(reaped.failed))
    if reaped.cancelled:
        logger.info("收敛已请求取消的 run", count=len(reaped.cancelled))
    return len(reaped.failed) + len(reaped.recovered_cowork) + len(reaped.cancelled)


async def cost_sweeper_tick(ctx: dict[str, Any]) -> int:
    """过期未结算的预留按上限落账, 否则额度会被永久占住。"""

    del ctx  # 费用预留在自己的 SQLite 库里，不需要业务 session
    swept = await default_telemetry_store().sweep_expired()
    if swept:
        logger.warning("按上限结算过期费用预留", count=swept)
    return swept


async def memory_dispatch_tick(ctx: dict[str, Any]) -> int:
    """补偿作业已落库但首次入队失败的窗口，并收敛重试耗尽的作业。"""

    settings = ctx["settings"]
    if not settings.memory_extraction_enabled:
        return 0
    jobs = await list_dispatchable_memory_jobs(max_attempts=settings.memory_job_max_attempts)
    queue = ctx.get("run_queue") or await get_run_queue()
    for job_id, attempt in jobs:
        await queue.enqueue_memory_job(job_id, attempt=attempt)
    return len(jobs)


async def skill_distillation_dispatch_tick(ctx: dict[str, Any]) -> int:
    """补偿 Skill 蒸馏作业首次入队失败与失联租约。

    队列在候选目录里，不需要业务 session：一次扫描就是一次 listdir。
    """

    settings = ctx["settings"]
    if not settings.skill_distillation_enabled:
        return 0
    jobs = await asyncio.to_thread(
        list_dispatchable_skill_jobs,
        settings.cowork_skill_candidates_path,
        max_attempts=settings.skill_distillation_job_max_attempts,
        lease_s=settings.skill_distillation_job_lease_s,
    )
    queue = ctx.get("run_queue") or await get_run_queue()
    for run_id, attempt in jobs:
        await queue.enqueue_skill_job(run_id, attempt=attempt)
    return len(jobs)


async def scheduler_dispatch_tick(ctx: dict[str, Any]) -> int:
    """补跑到期计划，并补偿“已落 DB、Redis 首次入队失败”的窗口。"""

    settings = ctx["settings"]
    if not settings.cowork_enabled:
        return 0
    session_factory = ctx["session_factory"]
    first_tick = not bool(ctx.get("cowork_scheduler_started"))
    async with session_factory() as session:
        created = await dispatch_due_schedules(
            session,
            settings=settings,
            trigger="catchup" if first_tick else "schedule",
        )
        # 自唤醒和计划派发共用这一个 tick：两者都是"到点了把 run 放进队列"。
        woken = await claim_due_sleeping_runs(session)
        await session.commit()
        dispatchable = [*await list_dispatchable_scheduled_runs(session), *woken]
    ctx["cowork_scheduler_started"] = True
    queue = ctx.get("run_queue") or await get_run_queue()
    bus: RunBus = ctx["bus"]
    enqueued = 0
    for run_id in dispatchable:
        try:
            await queue.enqueue_cowork_run(run_id)
        except Exception:
            logger.exception("自动化 run 入队失败，等待下次 tick 补偿", run_id=str(run_id))
            continue
        await bus.publish(run_id)
        enqueued += 1
    if created:
        logger.info("创建到期的 Cowork 自动化 run", count=len(created))
    return enqueued
