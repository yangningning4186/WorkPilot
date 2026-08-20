"""Agent 计划、尝试与 checkpoint 的 PostgreSQL 投影。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.agent.state import AgentState, PlanStepState, json_state
from app.cowork_store.routing import configured_cowork_store


@dataclass(frozen=True)
class AgentCheckpoint:
    run_id: UUID
    checkpoint_id: str
    parent_id: str | None
    state: AgentState


async def ensure_plan(
    session: AsyncSession,
    *,
    run_id: UUID,
    steps: list[PlanStepState],
) -> None:
    for step in steps:
        await session.execute(
            text(
                """
                INSERT INTO agent_plan_steps
                    (id, run_id, step_idx, description, tool, depends_on, status)
                VALUES (:id, :run_id, :step_idx, :description, :tool, :depends_on, :status)
                ON CONFLICT (run_id, step_idx) DO NOTHING
                """
            ),
            {
                "id": UUID(step["id"]),
                "run_id": run_id,
                "step_idx": step["idx"],
                "description": step["description"],
                "tool": step["tool"],
                "depends_on": step["depends_on"],
                "status": step["status"],
            },
        )
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT id, step_idx, description, tool, depends_on, status
                    FROM agent_plan_steps WHERE run_id = :run_id ORDER BY step_idx
                    """
                ),
                {"run_id": run_id},
            )
        )
        .mappings()
        .all()
    )
    expected = [
        (
            step["id"],
            step["idx"],
            step["description"],
            step["tool"],
            step["depends_on"],
        )
        for step in steps
    ]
    actual = [
        (
            str(row["id"]),
            int(row["step_idx"]),
            str(row["description"]),
            row["tool"],
            list(row["depends_on"] or []),
        )
        for row in rows
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
    if status not in {"pending", "running", "done", "failed", "skipped"}:
        raise ValueError(f"非法 plan step 状态: {status}")
    store = configured_cowork_store()
    if store is not None and await store.get_run(run_id) is not None:
        await store.update_plan_step_status(run_id=run_id, step_id=step_id, status=status)
        return
    updated = (
        await session.execute(
            text(
                """
                UPDATE agent_plan_steps SET status = :status
                WHERE id = :step_id AND run_id = :run_id
                RETURNING id
                """
            ),
            {"run_id": run_id, "step_id": step_id, "status": status},
        )
    ).scalar_one_or_none()
    if updated is None:
        raise LookupError(f"Agent plan step 不存在: {step_id}")


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
    store = configured_cowork_store()
    if store is not None and await store.get_run(run_id) is not None:
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
    attempt_id = uuid7()
    await session.execute(
        text(
            """
            INSERT INTO agent_attempts
                (id, run_id, plan_step_id, attempt_no, node, tool_name, tool_args,
                 tool_result, status, idempotency_key, latency_ms, tokens, error_model)
            VALUES
                (:id, :run_id, :plan_step_id, :attempt_no, :node, :tool_name,
                 CAST(:tool_args AS jsonb), CAST(:tool_result AS jsonb), :status,
                 :idempotency_key, :latency_ms, :tokens, :error_model)
            """
        ),
        {
            "id": attempt_id,
            "run_id": run_id,
            "plan_step_id": plan_step_id,
            "attempt_no": attempt_no,
            "node": node,
            "tool_name": tool_name,
            "tool_args": None if tool_args is None else json.dumps(tool_args, ensure_ascii=False),
            "tool_result": (
                None if tool_result is None else json.dumps(tool_result, ensure_ascii=False)
            ),
            "status": status,
            "idempotency_key": idempotency_key,
            "latency_ms": latency_ms,
            "tokens": tokens,
            "error_model": error_model,
        },
    )
    return attempt_id


async def next_attempt_no(
    session: AsyncSession,
    *,
    run_id: UUID,
    plan_step_id: UUID,
    node: str,
) -> int:
    store = configured_cowork_store()
    if store is not None and await store.get_run(run_id) is not None:
        return await store.next_attempt_no(run_id=run_id, plan_step_id=plan_step_id, node=node)
    latest = (
        await session.execute(
            text(
                """
                SELECT COALESCE(max(attempt_no), 0)
                FROM agent_attempts
                WHERE run_id = :run_id AND plan_step_id = :plan_step_id AND node = :node
                """
            ),
            {"run_id": run_id, "plan_step_id": plan_step_id, "node": node},
        )
    ).scalar_one()
    return int(latest) + 1


async def save_checkpoint(
    session: AsyncSession,
    *,
    run_id: UUID,
    state: AgentState,
    parent_id: str | None,
) -> AgentCheckpoint:
    clean = json_state(state)
    checkpoint_id = str(uuid7())
    await session.execute(
        text(
            """
            INSERT INTO agent_checkpoints (run_id, checkpoint_id, parent_id, state)
            VALUES (:run_id, :checkpoint_id, :parent_id, CAST(:state AS jsonb))
            """
        ),
        {
            "run_id": run_id,
            "checkpoint_id": checkpoint_id,
            "parent_id": parent_id,
            "state": json.dumps(clean, ensure_ascii=False, separators=(",", ":")),
        },
    )
    return AgentCheckpoint(run_id, checkpoint_id, parent_id, clean)


async def load_latest_checkpoint(session: AsyncSession, *, run_id: UUID) -> AgentCheckpoint | None:
    row = (
        (
            await session.execute(
                text(
                    """
                    SELECT run_id, checkpoint_id, parent_id, state
                    FROM agent_checkpoints
                    WHERE run_id = :run_id
                    ORDER BY created_at DESC, checkpoint_id DESC
                    LIMIT 1
                    """
                ),
                {"run_id": run_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return AgentCheckpoint(
        run_id=UUID(str(row["run_id"])),
        checkpoint_id=str(row["checkpoint_id"]),
        parent_id=None if row["parent_id"] is None else str(row["parent_id"]),
        state=json_state(row["state"]),
    )
