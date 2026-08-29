"""`tool_invocations` 副作用幂等租约协议（约束 9 / ADR-0007）。

四个函数就是那条约束的全部实现：`acquire_invocation` 抢租约、`complete_invocation`
落结果、`fail_invocation` 释放、`mark_invocation_outcome_unknown` 将可能已经发生的外部
副作用封为不可重放终态。**幂等落在这一层而不是 Agent 的 cursor 上**——
interrupt 或崩溃恢复可能重新进入尚未确认完成的执行片段，状态恢复不等于副作用不重放。

与 `runs.py` / `checkpoints.py` 同层同形状：Postgres 为主，桌面 Cowork 走 SQLite 旁路。
纯身份计算（`invocation_identity`）在 `app/agent_core/idempotency.py`，那部分无副作用、
无存储，属于框架层。

此前这三个函数住在 `app/rag/review/write_note.py` 里，Cowork 反过来从综述工作流
import 它们——ADR-0011 Step 3 把它挪到两个产品共同的下游。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.agent_core.contracts import InvocationLease
from app.core.db import DbSession as AsyncSession
from app.cowork_store.routing import cowork_store


async def acquire_invocation(
    session: AsyncSession,
    *,
    run_id: UUID,
    plan_step_id: UUID,
    tool_name: str,
    args: dict[str, Any],
    worker_id: str,
    lease_s: int,
) -> InvocationLease:
    """INSERT 或 CAS 回收副作用租约；禁止先读后写。"""

    if lease_s <= 0:
        raise ValueError("副作用租约必须大于 0 秒")
    store = cowork_store()
    return await store.acquire_invocation(
        run_id=run_id,
        plan_step_id=plan_step_id,
        tool_name=tool_name,
        args=args,
        worker_id=worker_id,
        lease_s=lease_s,
    )


async def complete_invocation(
    session: AsyncSession,
    *,
    key: str,
    worker_id: str,
    result: dict[str, Any],
    effect_ref: str,
) -> None:
    store = cowork_store()
    await store.complete_invocation(
        key=key, worker_id=worker_id, result=result, effect_ref=effect_ref
    )
    return


async def fail_invocation(session: AsyncSession, *, key: str, worker_id: str, error: str) -> None:
    store = cowork_store()
    await store.fail_invocation(key=key, worker_id=worker_id, error=error)
    return


async def mark_invocation_outcome_unknown(
    session: AsyncSession,
    *,
    key: str,
    worker_id: str,
) -> None:
    """Persist a terminal, non-replayable outcome without recording transport diagnostics."""

    store = cowork_store()
    await store.mark_invocation_outcome_unknown(key=key, worker_id=worker_id)
    return
