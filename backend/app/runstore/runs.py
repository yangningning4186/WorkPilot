"""run 生命周期与事件溯源。

`run_events` 是流式输出的唯一真相源(ADR-0007): 实时推送和刷新回放读同一份数据,
因此不存在"实时看到的和刷新后看到的不一致"。普通问答同样走 run, 让刷新恢复、
断线续传、时间线渲染只有一套实现。
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.agent_core.contracts import (
    TERMINAL_RUN_STATUSES as TERMINAL_RUN_STATUSES,
)
from app.agent_core.contracts import (
    RunEvent as RunEvent,
)
from app.agent_core.contracts import (
    RunRecord as RunRecord,
)
from app.agent_core.contracts import (
    WorkflowType as WorkflowType,
)
from app.agent_core.errors import RunNotFoundError as RunNotFoundError
from app.cowork_store.jsonl import JsonlMessage
from app.cowork_store.routing import configured_cowork_store

# 内联进 SQL 的常量白名单, 不接受外部输入。
_TERMINAL_SQL = "(" + ", ".join(f"'{status}'" for status in sorted(TERMINAL_RUN_STATUSES)) + ")"
MESSAGE_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


_RUN_COLUMNS = """
    id, conversation_id, goal, status, worker_id, lease_until, cancel_requested_at,
    budget_tokens, budget_calls, budget_wall_ms, used_tokens, used_calls, next_seq, error,
    answer_mode, workflow_type, schedule_id, unattended, run_trigger, retrieval_top_k
"""
_RUN_COLUMNS_QUALIFIED = ", ".join(f"ar.{column.strip()}" for column in _RUN_COLUMNS.split(","))


async def ensure_conversation(
    session: AsyncSession,
    *,
    conversation_id: UUID | None = None,
    scope: str = "local_owner",
    demo_session_id: UUID | None = None,
    title: str | None = None,
) -> UUID:
    """复用已有对话或新建一个。demo 作用域必须带 session, 由建表约束保证。"""

    if scope not in {"local_owner", "demo"}:
        raise ValueError("未知 conversation scope")
    if scope == "local_owner" and demo_session_id is not None:
        raise ValueError("local_owner 对话不能绑定 demo session")
    if scope == "demo" and demo_session_id is None:
        raise ValueError("demo 对话必须绑定 demo session")

    store = configured_cowork_store() if scope == "local_owner" else None
    if store is not None:
        if conversation_id is not None:
            active = await store.list_conversation_metadata(
                conversation_id=conversation_id, archived=False, limit=1
            )
            if not active:
                if await store.conversation_exists(conversation_id):
                    raise LookupError("对话已归档，请先恢复")
                raise LookupError(f"对话不存在: {conversation_id}")
            return conversation_id
        return await store.create_conversation(title=title)

    if conversation_id is not None:
        statement = """
            SELECT id FROM conversations
            WHERE id = :id
              AND scope = :scope
              AND archived_at IS NULL
              AND (
                (:scope = 'local_owner' AND demo_session_id IS NULL)
                OR (:scope = 'demo' AND demo_session_id = :demo_session_id)
              )
        """
        parameters = {
            "id": conversation_id,
            "scope": scope,
            "demo_session_id": demo_session_id,
        }
        found = (await session.execute(text(statement), parameters)).scalar_one_or_none()
        if found is None:
            raise LookupError(f"对话不存在: {conversation_id}")
        return UUID(str(found))

    new_id = uuid7()
    await session.execute(
        text(
            """
            INSERT INTO conversations (id, scope, demo_session_id, title)
            VALUES (:id, :scope, :demo_session_id, :title)
            """
        ),
        {
            "id": new_id,
            "scope": scope,
            "demo_session_id": demo_session_id,
            "title": title,
        },
    )
    return new_id


async def create_run(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    goal: str,
    budget_tokens: int,
    budget_calls: int,
    budget_wall_ms: int,
    answer_mode: str = "grounded",
    retrieval_top_k: int = 5,
    workflow_type: WorkflowType = "answer",
    schedule_id: UUID | None = None,
    unattended: bool = False,
    run_trigger: Literal["manual", "schedule", "catchup"] = "manual",
) -> RunRecord:
    if not goal.strip():
        raise ValueError("run 目标不能为空")
    if answer_mode not in {"grounded", "general"}:
        raise ValueError("answer_mode 只能是 grounded 或 general")
    if workflow_type not in {"answer", "literature_review", "cowork"}:
        raise ValueError("workflow_type 只能是 answer、literature_review 或 cowork")
    if not 1 <= retrieval_top_k <= 20:
        raise ValueError("retrieval_top_k 必须位于 1 到 20")
    if run_trigger not in {"manual", "schedule", "catchup"}:
        raise ValueError("run_trigger 无效")
    if schedule_id is not None and workflow_type != "cowork":
        raise ValueError("只有 Cowork run 可以关联 schedule")
    store = configured_cowork_store()
    if store is not None and workflow_type == "cowork":
        return await store.create_run(
            conversation_id=conversation_id,
            goal=goal,
            budget_tokens=budget_tokens,
            budget_calls=budget_calls,
            budget_wall_ms=budget_wall_ms,
            answer_mode=answer_mode,  # type: ignore[arg-type]
            retrieval_top_k=retrieval_top_k,
            workflow_type=workflow_type,
            schedule_id=schedule_id,
            unattended=unattended,
            run_trigger=run_trigger,
        )
    run_id = uuid7()
    await session.execute(
        text(
            """
            INSERT INTO agent_runs
                (id, conversation_id, goal, status,
                 budget_tokens, budget_calls, budget_wall_ms, answer_mode, workflow_type,
                 schedule_id, unattended, run_trigger, retrieval_top_k)
            VALUES (:id, :conversation_id, :goal, 'queued',
                    :budget_tokens, :budget_calls, :budget_wall_ms, :answer_mode,
                    :workflow_type, :schedule_id, :unattended, :run_trigger, :retrieval_top_k)
            """
        ),
        {
            "id": run_id,
            "conversation_id": conversation_id,
            "goal": goal,
            "budget_tokens": budget_tokens,
            "budget_calls": budget_calls,
            "budget_wall_ms": budget_wall_ms,
            "answer_mode": answer_mode,
            "workflow_type": workflow_type,
            "schedule_id": schedule_id,
            "unattended": unattended,
            "run_trigger": run_trigger,
            "retrieval_top_k": retrieval_top_k,
        },
    )
    run = await get_run(session, run_id)
    if run is None:  # pragma: no cover - 同事务内必然可见
        raise RunNotFoundError(str(run_id))
    return run


async def get_run(session: AsyncSession, run_id: UUID) -> RunRecord | None:
    store = configured_cowork_store()
    if store is not None:
        local = await store.get_run(run_id)
        if local is not None:
            return local
    row = (
        (
            await session.execute(
                text(f"SELECT {_RUN_COLUMNS} FROM agent_runs WHERE id = :id"),
                {"id": run_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else RunRecord(**row)


async def get_run_for_demo_session(
    session: AsyncSession,
    *,
    run_id: UUID,
    demo_session_id: UUID,
) -> RunRecord | None:
    """按 conversation 所有权读取 run；不存在与越权对调用方均表现为 None。"""

    row = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT {_RUN_COLUMNS_QUALIFIED}
                    FROM agent_runs ar
                    JOIN conversations c ON c.id = ar.conversation_id
                    WHERE ar.id = :run_id
                      AND c.scope = 'demo'
                      AND c.demo_session_id = :demo_session_id
                    """
                ),
                {"run_id": run_id, "demo_session_id": demo_session_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else RunRecord(**row)


async def get_run_for_identity(
    session: AsyncSession,
    *,
    run_id: UUID,
    scope: str,
    demo_session_id: UUID | None,
) -> RunRecord | None:
    """按服务端解析出的身份读取 run，owner 与 demo 空间严格隔离。"""

    if scope not in {"local_owner", "demo"}:
        raise ValueError("未知 identity scope")
    store = configured_cowork_store() if scope == "local_owner" else None
    if store is not None:
        local = await store.get_run(run_id)
        if local is not None:
            return local
    row = (
        (
            await session.execute(
                text(
                    f"""
                    SELECT {_RUN_COLUMNS_QUALIFIED}
                    FROM agent_runs ar
                    JOIN conversations c ON c.id = ar.conversation_id
                    WHERE ar.id = :run_id
                      AND c.scope = :scope
                      AND (
                        (:scope = 'local_owner' AND c.demo_session_id IS NULL)
                        OR (:scope = 'demo' AND c.demo_session_id = :demo_session_id)
                      )
                    """
                ),
                {
                    "run_id": run_id,
                    "scope": scope,
                    "demo_session_id": demo_session_id,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else RunRecord(**row)


async def demo_session_can_access_version(
    session: AsyncSession,
    *,
    version_id: UUID,
    demo_session_id: UUID,
) -> bool:
    """只允许读取当前 session 自己收到过 citation 的文档版本。"""

    return bool(
        (
            await session.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM run_events re
                        JOIN agent_runs ar ON ar.id = re.run_id
                        JOIN conversations c ON c.id = ar.conversation_id
                        WHERE re.type = 'citation'
                          AND re.payload ->> 'version_id' = :version_id
                          AND c.scope = 'demo'
                          AND c.demo_session_id = :demo_session_id
                    )
                    """
                ),
                {"version_id": str(version_id), "demo_session_id": demo_session_id},
            )
        ).scalar_one()
    )


async def identity_can_access_version(
    session: AsyncSession,
    *,
    version_id: UUID,
    scope: str,
    demo_session_id: UUID | None,
) -> bool:
    if scope not in {"local_owner", "demo"}:
        raise ValueError("未知 identity scope")
    return bool(
        (
            await session.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM run_events re
                        JOIN agent_runs ar ON ar.id = re.run_id
                        JOIN conversations c ON c.id = ar.conversation_id
                        WHERE re.type = 'citation'
                          AND re.payload ->> 'version_id' = :version_id
                          AND c.scope = :scope
                          AND (
                            (:scope = 'local_owner' AND c.demo_session_id IS NULL)
                            OR (:scope = 'demo' AND c.demo_session_id = :demo_session_id)
                          )
                    )
                    """
                ),
                {
                    "version_id": str(version_id),
                    "scope": scope,
                    "demo_session_id": demo_session_id,
                },
            )
        ).scalar_one()
    )


async def append_events(
    session: AsyncSession,
    *,
    run_id: UUID,
    events: Sequence[tuple[str, dict[str, Any]]],
) -> list[RunEvent]:
    """原子发号并落库。

    发号走 `UPDATE ... RETURNING` 而不是 `MAX(seq)+1`: 后者在并发下会发出重复号,
    而 seq 连续正是断线续传的前提。单 run 由单 worker 处理, 这里不会有竞争,
    但 watchdog 也会写事件, 所以仍然按并发安全写。
    """

    if not events:
        return []
    store = configured_cowork_store()
    if store is not None and await store.get_run(run_id) is not None:
        return await store.append_events(run_id=run_id, events=events)
    allocated_end = (
        await session.execute(
            text(
                """
                UPDATE agent_runs
                SET next_seq = next_seq + :count, updated_at = now()
                WHERE id = :run_id
                RETURNING next_seq
                """
            ),
            {"run_id": run_id, "count": len(events)},
        )
    ).scalar_one_or_none()
    if allocated_end is None:
        raise RunNotFoundError(str(run_id))

    first_seq = int(allocated_end) - len(events)
    # 单条多值 INSERT 而不是 executemany: 后者拿不到 RETURNING 的行,
    # 而调用方需要数据库侧的 created_at 与最终 seq。
    params: dict[str, Any] = {"run_id": run_id}
    values: list[str] = []
    for offset, (event_type, payload) in enumerate(events):
        params[f"seq_{offset}"] = first_seq + offset
        params[f"type_{offset}"] = event_type
        params[f"payload_{offset}"] = json.dumps(payload, ensure_ascii=False)
        values.append(f"(:run_id, :seq_{offset}, :type_{offset}, CAST(:payload_{offset} AS jsonb))")
    created = (
        (
            await session.execute(
                text(
                    f"""
                    INSERT INTO run_events (run_id, seq, type, payload)
                    VALUES {", ".join(values)}
                    RETURNING run_id, seq, type, payload, created_at
                    """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    return sorted((RunEvent(**row) for row in created), key=lambda event: event.seq)


async def list_events(
    session: AsyncSession,
    *,
    run_id: UUID,
    after_seq: int = 0,
    limit: int = 200,
) -> list[RunEvent]:
    if after_seq < 0:
        raise ValueError("after_seq 不能为负数")
    if not 1 <= limit <= 1000:
        raise ValueError("limit 必须位于 1 到 1000")
    store = configured_cowork_store()
    if store is not None and await store.get_run(run_id) is not None:
        return (await store.list_events(run_id=run_id, after_seq=after_seq))[:limit]
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT run_id, seq, type, payload, created_at
                    FROM run_events
                    WHERE run_id = :run_id AND seq > :after_seq
                    ORDER BY seq
                    LIMIT :limit
                    """
                ),
                {"run_id": run_id, "after_seq": after_seq, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [RunEvent(**row) for row in rows]


async def claim_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    worker_id: str,
    lease_s: int,
) -> RunRecord | None:
    """抢占 run 并取得租约; 抢不到返回 None。

    条件 UPDATE 而不是"先读后写": 队列重投递时两个 worker 会同时看到 queued,
    先读后写必然双跑, 于是同一次回答被计费两遍。
    """

    if lease_s <= 0:
        raise ValueError("租约时长必须大于 0")
    store = configured_cowork_store()
    if store is not None and await store.get_run(run_id) is not None:
        return await store.claim_run(run_id=run_id, worker_id=worker_id, lease_s=lease_s)
    row = (
        (
            await session.execute(
                text(
                    f"""
                    UPDATE agent_runs
                    SET status = 'executing',
                        worker_id = :worker_id,
                        lease_until = now() + make_interval(secs => :lease_s),
                        heartbeat_at = now(),
                        started_at = COALESCE(started_at, now()),
                        updated_at = now()
                    WHERE id = :run_id
                      AND cancel_requested_at IS NULL
                      AND (
                            status = 'queued'
                            OR (status NOT IN {_TERMINAL_SQL} AND lease_until < now())
                          )
                    RETURNING {_RUN_COLUMNS}
                    """
                ),
                {"run_id": run_id, "worker_id": worker_id, "lease_s": lease_s},
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else RunRecord(**row)


async def renew_lease(
    session: AsyncSession,
    *,
    run_id: UUID,
    worker_id: str,
    lease_s: int,
) -> RunRecord | None:
    """续租并返回最新状态; 租约已被别人接管时返回 None。"""

    store = configured_cowork_store()
    if store is not None and await store.get_run(run_id) is not None:
        renewed = await store.renew_run_lease(run_id=run_id, worker_id=worker_id, lease_s=lease_s)
        return await store.get_run(run_id) if renewed else None

    row = (
        (
            await session.execute(
                text(
                    f"""
                    UPDATE agent_runs
                    SET lease_until = now() + make_interval(secs => :lease_s),
                        heartbeat_at = now(),
                        updated_at = now()
                    WHERE id = :run_id AND worker_id = :worker_id AND lease_until > now()
                    RETURNING {_RUN_COLUMNS}
                    """
                ),
                {"run_id": run_id, "worker_id": worker_id, "lease_s": lease_s},
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else RunRecord(**row)


async def add_run_usage(
    session: AsyncSession,
    *,
    run_id: UUID,
    used_tokens: int,
    used_calls: int,
) -> None:
    """把一段执行的实际消耗累加进 run 行。

    只累加增量, 由调用方保证与 checkpoint 在同一个事务里提交: 节点中途崩溃时
    消耗与 checkpoint 一起回滚, 恢复后重跑该节点会重新计费——漏记上限是一个节点,
    而不是整个 run。
    """

    if used_tokens < 0 or used_calls < 0:
        raise ValueError("run 用量增量不能为负")
    if used_tokens == 0 and used_calls == 0:
        return
    store = configured_cowork_store()
    if store is not None and await store.get_run(run_id) is not None:
        await store.add_run_usage(run_id=run_id, used_tokens=used_tokens, used_calls=used_calls)
        return
    await session.execute(
        text(
            """
            UPDATE agent_runs
            SET used_tokens = used_tokens + :used_tokens,
                used_calls = used_calls + :used_calls,
                updated_at = now()
            WHERE id = :run_id
            """
        ),
        {"run_id": run_id, "used_tokens": used_tokens, "used_calls": used_calls},
    )


async def finish_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    status: str,
    worker_id: str | None = None,
    error: str | None = None,
    used_tokens: int = 0,
    used_calls: int = 0,
) -> bool:
    """把 run 落终态。worker_id 非空时只允许租约持有者收尾。"""

    if status not in TERMINAL_RUN_STATUSES:
        raise ValueError(f"不是终态: {status}")
    store = configured_cowork_store()
    if store is not None and await store.get_run(run_id) is not None:
        return await store.finish_run(
            run_id=run_id,
            status=status,
            worker_id=worker_id,
            error=error,
            used_tokens=used_tokens,
            used_calls=used_calls,
        )
    updated = (
        await session.execute(
            text(
                """
                UPDATE agent_runs
                SET status = :status,
                    error = :error,
                    used_tokens = used_tokens + :used_tokens,
                    used_calls = used_calls + :used_calls,
                    finished_at = now(),
                    lease_until = NULL,
                    updated_at = now()
                WHERE id = :run_id
                  AND status <> :status
                  AND (CAST(:worker_id AS text) IS NULL OR worker_id = :worker_id)
                RETURNING id
                """
            ),
            {
                "run_id": run_id,
                "status": status,
                "error": error,
                "worker_id": worker_id,
                "used_tokens": used_tokens,
                "used_calls": used_calls,
            },
        )
    ).scalar_one_or_none()
    return updated is not None


async def request_cancel(
    session: AsyncSession,
    *,
    run_id: UUID,
    scope: str | None = None,
    demo_session_id: UUID | None = None,
) -> RunRecord:
    """请求取消。

    还没被 worker 领走就直接落终态; 已经在跑则只打标记, 由 worker 在下一个检查点
    自己收尾——否则会出现"状态已终态但事件还在继续写"的不一致。
    """

    store = configured_cowork_store() if scope in {None, "local_owner"} else None
    if store is not None and await store.get_run(run_id) is not None:
        return await store.request_cancel(run_id=run_id)

    row = (
        (
            await session.execute(
                text(
                    f"""
                    UPDATE agent_runs
                    SET cancel_requested_at = COALESCE(cancel_requested_at, now()),
                        status = CASE
                            WHEN status IN ('queued', 'waiting_human') THEN 'cancelled'
                            ELSE status
                        END,
                        finished_at = CASE
                            WHEN status IN ('queued', 'waiting_human') THEN now()
                            ELSE finished_at
                        END,
                        updated_at = now()
                    WHERE id = :run_id
                      AND (
                        CAST(:scope AS text) IS NULL
                        OR EXISTS (
                            SELECT 1 FROM conversations c
                            WHERE c.id = agent_runs.conversation_id
                              AND c.scope = :scope
                              AND (
                                (:scope = 'local_owner' AND c.demo_session_id IS NULL)
                                OR (:scope = 'demo' AND c.demo_session_id = :demo_session_id)
                              )
                        )
                      )
                    RETURNING {_RUN_COLUMNS}
                    """
                ),
                {"run_id": run_id, "scope": scope, "demo_session_id": demo_session_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise RunNotFoundError(str(run_id))
    return RunRecord(**row)


@dataclass(frozen=True)
class ReapedRuns:
    """一次 watchdog 扫描的两类结果。

    `recovered` 被退回 `queued` 并计数, 等待重新入队后由 `claim_run` 抢占;
    `failed` 已经落终态。

    退回 `queued` 而不是只清空 `lease_until`: `claim_run` 的条件是
    `status = 'queued' OR (非终态 AND lease_until < now())`, 而 `NULL < now()` 是 NULL
    不是 true —— 只清租约会让 run 变成谁都抢不到的僵尸。
    """

    failed: list[UUID]
    recovered: list[tuple[UUID, int]]
    recovered_cowork: list[tuple[UUID, int]]
    cancelled: list[UUID]


async def reap_expired_runs(
    session: AsyncSession, *, limit: int = 50, max_recovery: int = 3
) -> ReapedRuns:
    """回收租约过期的 run: 可恢复的松开租约等待重投, 其余明确标记为失败。

    普通流式回答不自动重试: 一次已经发出去的模型调用是否计费无法确认, 静默重放
    等于重复计费(docs/08 §3.1)。能自动恢复的只有**同时**满足三个条件的 run:
    `literature_review/cowork` 工作流、已经落过 checkpoint、且自动恢复次数没用完
    (ADR-0007)。前两条保证重跑从最近 checkpoint 继续且副作用走幂等协议,
    第三条保证一个稳定把 worker 拖垮的 run 不会被无限重投。
    """

    if limit < 1:
        raise ValueError("limit 必须大于 0")
    if max_recovery < 0:
        raise ValueError("自动恢复次数上限不能为负")
    store = configured_cowork_store()
    local_failed: list[UUID] = []
    local_recovered: list[tuple[UUID, int]] = []
    local_cancelled: list[UUID] = []
    if store is not None:
        local = await store.reap_expired_runs(limit=limit, max_recovery=max_recovery)
        local_failed = local["failed"]
        local_recovered = local["recovered"]
        local_cancelled = local["cancelled"]
        for run_id, attempt in local_recovered:
            await append_events(
                session,
                run_id=run_id,
                events=[
                    (
                        "step.update",
                        {
                            "status": "recovering",
                            "summary": f"worker 失联，正在从最近 checkpoint 恢复（第 {attempt} 次）。",
                        },
                    )
                ],
            )
        for run_id in local_failed:
            await append_events(
                session,
                run_id=run_id,
                events=[
                    (
                        "error",
                        {
                            "code": "worker_lease_expired",
                            "retryable": True,
                            "user_message": "worker 失联，任务未能安全恢复。",
                        },
                    )
                ],
            )
        for run_id in local_cancelled:
            await append_events(
                session,
                run_id=run_id,
                events=[
                    (
                        "error",
                        {
                            "code": "cancelled",
                            "retryable": True,
                            "user_message": "任务已取消。",
                        },
                    ),
                    ("run.done", {"workflow_type": "cowork", "status": "cancelled"}),
                ],
            )
    # SQLite 只承载 Cowork 控制面；answer / literature_review 仍在 PostgreSQL。
    # 因此不能在处理完本地 store 后提前返回，且下面的 SQL 必须只排除已由
    # SQLite 负责的 cowork 行，继续收敛 PostgreSQL 中的其他工作流。
    postgres_workflow_filter = "AND workflow_type <> 'cowork'" if store is not None else ""
    # 取消请求可能恰好撞上 worker 退出或人工交互恢复。此时既没有活 worker 收尾，
    # 也不能把它重新排队；watchdog 负责把这种半终止状态收敛为明确终态。
    cancelled_rows = (
        (
            await session.execute(
                text(
                    f"""
                    UPDATE agent_runs
                    SET status = 'cancelled', finished_at = now(), worker_id = NULL,
                        lease_until = NULL, heartbeat_at = NULL, updated_at = now()
                    WHERE id IN (
                        SELECT id FROM agent_runs
                        WHERE cancel_requested_at IS NOT NULL
                          {postgres_workflow_filter}
                          AND status NOT IN ('completed', 'failed', 'cancelled', 'budget_exceeded')
                          AND (
                            status IN ('queued', 'waiting_human')
                            OR (status = 'executing' AND (lease_until IS NULL OR lease_until < now()))
                          )
                        ORDER BY updated_at
                        LIMIT :limit
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id, workflow_type
                    """
                ),
                {"limit": limit},
            )
        )
        .mappings()
        .all()
    )
    cancelled = [UUID(str(row["id"])) for row in cancelled_rows]
    for row in cancelled_rows:
        run_id = UUID(str(row["id"]))
        if row["workflow_type"] == "cowork":
            await session.execute(
                text(
                    """
                    UPDATE cowork_inbox_items
                    SET status = 'cancelled', responded_at = COALESCE(responded_at, now())
                    WHERE run_id = :run_id AND status = 'pending'
                    """
                ),
                {"run_id": run_id},
            )
        await session.execute(
            text(
                """
                UPDATE messages
                SET status = 'cancelled', content = CASE
                    WHEN role = 'assistant' AND content = '' THEN 'Cowork 任务已停止。'
                    ELSE content
                END, updated_at = now()
                WHERE run_id = :run_id AND status = 'streaming'
                """
            ),
            {"run_id": run_id},
        )
        await append_events(
            session,
            run_id=run_id,
            events=[
                (
                    "error",
                    {
                        "code": "cancelled",
                        "retryable": True,
                        "user_message": "任务已取消。",
                    },
                ),
                (
                    "run.done",
                    {"workflow_type": row["workflow_type"], "status": "cancelled"},
                ),
            ],
        )
    recoverable_workflows = (
        "ar.workflow_type = 'literature_review'"
        if store is not None
        else "ar.workflow_type IN ('literature_review', 'cowork')"
    )
    recoverable_predicate = f"""
        {recoverable_workflows}
        AND ar.cancel_requested_at IS NULL
        AND ar.recovery_count < :max_recovery
        AND EXISTS (
            SELECT 1 FROM agent_checkpoints ac WHERE ac.run_id = ar.id
        )
    """
    recovered_rows = (
        (
            await session.execute(
                text(
                    f"""
                    UPDATE agent_runs
                    SET status = 'queued',
                        recovery_count = recovery_count + 1,
                        worker_id = NULL,
                        lease_until = NULL,
                        heartbeat_at = NULL,
                        updated_at = now()
                    WHERE id IN (
                        SELECT ar.id FROM agent_runs ar
                        WHERE ar.status NOT IN {_TERMINAL_SQL}
                          AND ar.lease_until IS NOT NULL
                          AND ar.lease_until < now()
                          {"AND ar.workflow_type <> 'cowork'" if store is not None else ""}
                          AND {recoverable_predicate}
                        ORDER BY ar.lease_until
                        LIMIT :limit
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id, recovery_count, workflow_type
                    """
                ),
                {"limit": limit, "max_recovery": max_recovery},
            )
        )
        .mappings()
        .all()
    )
    recovered = [
        (UUID(str(row["id"])), int(row["recovery_count"]))
        for row in recovered_rows
        if row["workflow_type"] == "literature_review"
    ]
    recovered_cowork = [
        (UUID(str(row["id"])), int(row["recovery_count"]))
        for row in recovered_rows
        if row["workflow_type"] == "cowork"
    ]
    for run_id, attempt in recovered + recovered_cowork:
        await append_events(
            session,
            run_id=run_id,
            events=[
                (
                    "step.update",
                    {
                        "status": "recovering",
                        "summary": (
                            f"worker 失联，正在从最近 checkpoint 恢复（第 {attempt} 次）。"
                        ),
                        "recovery_count": attempt,
                    },
                )
            ],
        )
    run_ids = list(
        (
            await session.execute(
                text(
                    f"""
                    UPDATE agent_runs
                    SET status = 'failed',
                        error = 'worker 租约过期, 任务未完成',
                        finished_at = now(),
                        lease_until = NULL,
                        updated_at = now()
                    WHERE id IN (
                        SELECT ar.id FROM agent_runs ar
                        WHERE ar.status NOT IN {_TERMINAL_SQL}
                          AND ar.lease_until IS NOT NULL
                          AND ar.lease_until < now()
                          {"AND ar.workflow_type <> 'cowork'" if store is not None else ""}
                          AND NOT ({recoverable_predicate})
                        ORDER BY ar.lease_until
                        LIMIT :limit
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id
                    """
                ),
                {"limit": limit, "max_recovery": max_recovery},
            )
        )
        .scalars()
        .all()
    )
    for run_id in run_ids:
        await append_events(
            session,
            run_id=run_id,
            events=[
                (
                    "error",
                    {
                        "user_message": "回答意外中断, 请重新提问。",
                        "retryable": True,
                        "code": "worker_lease_expired",
                    },
                )
            ],
        )
        await session.execute(
            text(
                """
                UPDATE messages
                SET status = 'failed', updated_at = now()
                WHERE run_id = :run_id AND status = 'streaming'
                """
            ),
            {"run_id": run_id},
        )
    return ReapedRuns(
        failed=local_failed + [UUID(str(run_id)) for run_id in run_ids],
        recovered=recovered,
        recovered_cowork=local_recovered + recovered_cowork,
        cancelled=local_cancelled + cancelled,
    )


async def append_message(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    role: str,
    content: str = "",
    status: str = "completed",
    run_id: UUID | None = None,
    trace_id: str | None = None,
) -> UUID:
    """在对话末尾追加消息, seq 由数据库同事务计算。"""

    store = configured_cowork_store()
    if store is not None and await store.conversation_exists(conversation_id):
        from app.cowork_store.factory import local_cowork_stores

        message_id = uuid7()
        seq = await store.allocate_message(
            record_id=message_id,
            conversation_id=conversation_id,
            role=role,
            status=status,
            run_id=run_id,
            title_source=content,
        )
        await local_cowork_stores().conversations.append(
            JsonlMessage.create(
                record_id=message_id,
                conversation_id=conversation_id,
                seq=seq,
                role=role,  # type: ignore[arg-type]
                content=content,
                status=status,  # type: ignore[arg-type]
                run_id=run_id,
            )
        )
        return message_id

    message_id = uuid7()
    await session.execute(
        text(
            """
            INSERT INTO messages
                (id, conversation_id, seq, role, content, status, run_id, trace_id)
            SELECT :id, :conversation_id, COALESCE(MAX(seq), 0) + 1,
                   :role, :content, :status, :run_id, :trace_id
            FROM messages
            WHERE conversation_id = :conversation_id
            """
        ),
        {
            "id": message_id,
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "status": status,
            "run_id": run_id,
            "trace_id": trace_id,
        },
    )
    await session.execute(
        text(
            """
            UPDATE conversations
            SET updated_at = now(),
                title = CASE
                    WHEN :role = 'user' AND (title IS NULL OR title = '新会话')
                    THEN left(:content, 80)
                    ELSE title
                END
            WHERE id = :conversation_id
            """
        ),
        {"conversation_id": conversation_id, "role": role, "content": content.strip()},
    )
    return message_id


async def finalize_message(
    session: AsyncSession,
    *,
    message_id: UUID,
    status: str,
    content: str | None = None,
    citations: list[dict[str, Any]] | None = None,
) -> None:
    if status not in MESSAGE_TERMINAL_STATUSES:
        raise ValueError(f"不是消息终态: {status}")
    store = configured_cowork_store()
    if store is not None:
        from dataclasses import replace

        from app.cowork_store.factory import local_cowork_stores

        messages = local_cowork_stores().conversations
        conversation_id = await store.get_message_conversation_id(record_id=message_id)
        current = (
            None
            if conversation_id is None
            else await messages.find(message_id, conversation_id=conversation_id)
        )
        if current is not None:
            await messages.append(
                replace(
                    current,
                    status=status,  # type: ignore[arg-type]
                    content=current.content if content is None else content,
                    citations=current.citations if citations is None else tuple(citations),
                )
            )
            await store.update_message_status(record_id=message_id, status=status)
            return
    await session.execute(
        text(
            """
            UPDATE messages
            SET status = :status,
                content = COALESCE(:content, content),
                citations = COALESCE(CAST(:citations AS jsonb), citations),
                updated_at = now()
            WHERE id = :message_id
            """
        ),
        {
            "message_id": message_id,
            "status": status,
            "content": content,
            "citations": None if citations is None else json.dumps(citations, ensure_ascii=False),
        },
    )
