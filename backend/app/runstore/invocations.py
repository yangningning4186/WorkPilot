"""`tool_invocations` 副作用幂等租约协议（约束 9 / ADR-0007）。

三个函数就是那条约束的全部实现：`acquire_invocation` 抢租约、`complete_invocation`
落结果、`fail_invocation` 释放。**幂等落在这一层而不是 Agent 的 cursor 上**——
LangGraph 从 interrupt 恢复会从节点开头重跑，状态恢复不等于副作用不重放。

与 `runs.py` / `checkpoints.py` 同层同形状：Postgres 为主，桌面 Cowork 走 SQLite 旁路。
纯身份计算（`invocation_identity`）在 `app/agent_core/idempotency.py`，那部分无副作用、
无存储，属于框架层。

此前这三个函数住在 `app/rag/review/write_note.py` 里，Cowork 反过来从综述工作流
import 它们——ADR-0011 Step 3 把它挪到两个产品共同的下游。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_core.contracts import InvocationLease
from app.agent_core.idempotency import (
    InvocationInFlightError,
    canonical_json,
    invocation_identity,
)
from app.cowork_store.routing import configured_cowork_store


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
    store = configured_cowork_store()
    if store is not None and await store.get_run(run_id) is not None:
        return await store.acquire_invocation(
            run_id=run_id,
            plan_step_id=plan_step_id,
            tool_name=tool_name,
            args=args,
            worker_id=worker_id,
            lease_s=lease_s,
        )
    key, args_hash = invocation_identity(
        run_id=run_id,
        plan_step_id=plan_step_id,
        tool_name=tool_name,
        args=args,
    )
    inserted = (
        await session.execute(
            text(
                """
                INSERT INTO tool_invocations
                    (idempotency_key, run_id, tool_name, args_hash, status,
                     lease_owner, lease_until)
                VALUES
                    (:key, :run_id, :tool_name, :args_hash, 'in_flight',
                     :worker_id, now() + make_interval(secs => :lease_s))
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING idempotency_key
                """
            ),
            {
                "key": key,
                "run_id": run_id,
                "tool_name": tool_name,
                "args_hash": args_hash,
                "worker_id": worker_id,
                "lease_s": lease_s,
            },
        )
    ).scalar_one_or_none()
    if inserted is not None:
        return InvocationLease(key, acquired=True)

    recovered = (
        (
            await session.execute(
                text(
                    """
                    UPDATE tool_invocations
                    SET status = 'in_flight', lease_owner = :worker_id,
                        lease_until = now() + make_interval(secs => :lease_s),
                        retry_count = retry_count + 1, result = NULL,
                        completed_at = NULL, updated_at = now()
                    WHERE idempotency_key = :key AND args_hash = :args_hash
                      AND (status = 'failed'
                           OR (status = 'in_flight' AND lease_until < now()))
                    RETURNING idempotency_key
                    """
                ),
                {
                    "key": key,
                    "args_hash": args_hash,
                    "worker_id": worker_id,
                    "lease_s": lease_s,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if recovered is not None:
        return InvocationLease(key, acquired=True)

    existing = (
        (
            await session.execute(
                text(
                    """
                    SELECT args_hash, status, result, effect_ref
                    FROM tool_invocations WHERE idempotency_key = :key
                    """
                ),
                {"key": key},
            )
        )
        .mappings()
        .one()
    )
    if str(existing["args_hash"]) != args_hash:  # pragma: no cover - SHA-256 collision
        raise RuntimeError("幂等键碰撞：已有调用的参数摘要不同")
    if existing["status"] == "succeeded":
        result = existing["result"]
        return InvocationLease(
            key,
            acquired=False,
            result=result if isinstance(result, dict) else None,
            effect_ref=None if existing["effect_ref"] is None else str(existing["effect_ref"]),
        )
    raise InvocationInFlightError("相同工具调用正在执行，请稍后重试")


async def complete_invocation(
    session: AsyncSession,
    *,
    key: str,
    worker_id: str,
    result: dict[str, Any],
    effect_ref: str,
) -> None:
    store = configured_cowork_store()
    if store is not None and await store.has_invocation(key=key):
        await store.complete_invocation(
            key=key, worker_id=worker_id, result=result, effect_ref=effect_ref
        )
        return
    completed = (
        await session.execute(
            text(
                """
                UPDATE tool_invocations
                SET status = 'succeeded', result = CAST(:result AS jsonb),
                    effect_ref = :effect_ref, lease_owner = NULL, lease_until = NULL,
                    completed_at = now(), updated_at = now()
                WHERE idempotency_key = :key AND status = 'in_flight'
                  AND lease_owner = :worker_id
                RETURNING idempotency_key
                """
            ),
            {
                "key": key,
                "worker_id": worker_id,
                "result": canonical_json(result),
                "effect_ref": effect_ref,
            },
        )
    ).scalar_one_or_none()
    if completed is None:
        raise InvocationInFlightError("工具调用租约已被其他 worker 接管")


async def fail_invocation(session: AsyncSession, *, key: str, worker_id: str, error: str) -> None:
    store = configured_cowork_store()
    if store is not None and await store.has_invocation(key=key):
        await store.fail_invocation(key=key, worker_id=worker_id, error=error)
        return
    await session.execute(
        text(
            """
            UPDATE tool_invocations
            SET status = 'failed', result = CAST(:result AS jsonb),
                lease_owner = NULL, lease_until = NULL, updated_at = now()
            WHERE idempotency_key = :key AND status = 'in_flight'
              AND lease_owner = :worker_id
            """
        ),
        {
            "key": key,
            "worker_id": worker_id,
            "result": canonical_json({"error": error}),
        },
    )


