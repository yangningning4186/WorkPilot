"""定时维护: 回收失联 run, 落账过期费用预留。"""

from typing import Any

import structlog

from app.core.run_bus import RunBus
from app.services.cost_budget import sweep_expired_reservations
from app.services.runs import reap_expired_runs

logger = structlog.get_logger(__name__)


async def watchdog_tick(ctx: dict[str, Any]) -> int:
    """把租约过期的 run 明确标记为失败。

    不自动重跑: 一次已经发出去的模型调用是否计费无法确认, 静默重放等于重复计费。
    能自动恢复的只有带 checkpoint 且工具满足幂等边界的 Agent run(ADR-0007)。
    """

    bus: RunBus = ctx["bus"]
    session_factory = ctx["session_factory"]
    async with session_factory() as session:
        run_ids = await reap_expired_runs(session)
        await session.commit()
    for run_id in run_ids:
        # 唤醒还挂在 SSE 上的客户端, 让它们立刻读到 error 事件而不是干等心跳。
        await bus.publish(run_id)
    if run_ids:
        logger.warning("回收失联 run", count=len(run_ids))
    return len(run_ids)


async def cost_sweeper_tick(ctx: dict[str, Any]) -> int:
    """过期未结算的预留按上限落账, 否则额度会被永久占住。"""

    session_factory = ctx["session_factory"]
    async with session_factory() as session:
        swept = await sweep_expired_reservations(session)
    if swept:
        logger.warning("按上限结算过期费用预留", count=swept)
    return swept
