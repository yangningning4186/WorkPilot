"""Agent 计划、尝试与 checkpoint 的持久化投影（PostgreSQL + 桌面 SQLite 旁路）。

checkpoint 对状态的**内容**不作任何假设：`save_checkpoint` / `load_latest_checkpoint`
对状态类型泛型，只要求它过得了 `json_state`（约束 2）。综述与 Cowork 的状态形状
各自定义，本模块两边都不认识。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from app.agent_core.state import PLAN_STEP_STATUSES, PlanStepState, json_state
from app.core.db import DbSession as AsyncSession
from app.cowork_store.routing import cowork_store


@dataclass(frozen=True)
class AgentCheckpoint[StateT]:
    run_id: UUID
    checkpoint_id: str
    parent_id: str | None
    state: StateT


async def ensure_plan(
    session: AsyncSession,
    *,
    run_id: UUID,
    steps: list[PlanStepState],
) -> None:
    store = cowork_store()
    for step in steps:
        await store.upsert_plan_step(
            step_id=UUID(step["id"]),
            run_id=run_id,
            step_idx=step["idx"],
            description=step["description"],
            tool=step["tool"],
            status=step["status"],
        )
    existing = await store.list_plan_steps(run_id=run_id)
    expected = [(step["id"], step["idx"], step["description"], step["tool"]) for step in steps]
    actual = [
        (str(row["id"]), int(row["step_idx"]), str(row["description"]), row["tool"])
        for row in existing
    ]
    if actual != expected:
        raise ValueError("已存在的 Agent plan 与固定工作流定义漂移")


async def update_plan_step(
    session: AsyncSession,
    *,
    run_id: UUID,
    step_id: UUID,
    status: str,
) -> None:
    if status not in PLAN_STEP_STATUSES:
        raise ValueError(f"非法 plan step 状态: {status}")
    store = cowork_store()
    await store.update_plan_step_status(run_id=run_id, step_id=step_id, status=status)
    return


async def record_attempt(
    session: AsyncSession,
    *,
    run_id: UUID,
    plan_step_id: UUID | None,
    attempt_no: int,
    node: str,
    status: str,
    tool_name: str | None = None,
    tool_args: dict[str, Any] | None = None,
    tool_result: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    latency_ms: int | None = None,
    tokens: int | None = None,
    error_model: str | None = None,
) -> UUID:
    store = cowork_store()
    return await store.record_attempt(
        run_id=run_id,
        plan_step_id=plan_step_id,
        attempt_no=attempt_no,
        node=node,
        status=status,
        tool_name=tool_name,
        tool_args=tool_args,
        tool_result=tool_result,
        idempotency_key=idempotency_key,
        latency_ms=latency_ms,
        tokens=tokens,
        error_model=error_model,
    )


async def next_attempt_no(
    session: AsyncSession,
    *,
    run_id: UUID,
    plan_step_id: UUID,
    node: str,
) -> int:
    store = cowork_store()
    return await store.next_attempt_no(run_id=run_id, plan_step_id=plan_step_id, node=node)


async def save_checkpoint[StateT](
    session: AsyncSession,
    *,
    run_id: UUID,
    state: StateT,
    parent_id: str | None,
) -> AgentCheckpoint[StateT]:
    del session
    clean = json_state(state)
    saved = await cowork_store().save_checkpoint(
        run_id=run_id, state=cast("dict[str, Any]", clean), parent_id=parent_id
    )
    return AgentCheckpoint(run_id, saved.checkpoint_id, parent_id, clean)


async def load_latest_checkpoint[StateT](
    session: AsyncSession, *, run_id: UUID
) -> AgentCheckpoint[StateT] | None:
    del session
    checkpoint = await cowork_store().load_latest_checkpoint(run_id=run_id)
    if checkpoint is None:
        return None
    return AgentCheckpoint(
        run_id=run_id,
        checkpoint_id=checkpoint.checkpoint_id,
        parent_id=checkpoint.parent_id,
        state=json_state(cast("StateT", checkpoint.state)),
    )
