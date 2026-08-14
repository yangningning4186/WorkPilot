"""run 生命周期与事件溯源。

`run_events` 是流式输出的唯一真相源(ADR-0007): 实时推送和刷新回放读同一份数据,
因此不存在"实时看到的和刷新后看到的不一致"。普通问答同样走 run, 让刷新恢复、
断线续传、时间线渲染只有一套实现。
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

# 终态之后不再产生任何事件, SSE 可以安全断开。
TERMINAL_RUN_STATUSES = frozenset({"done", "failed", "cancelled", "budget_exceeded"})
# 内联进 SQL 的常量白名单, 不接受外部输入。
_TERMINAL_SQL = "(" + ", ".join(f"'{status}'" for status in sorted(TERMINAL_RUN_STATUSES)) + ")"
MESSAGE_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class RunNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class RunEvent:
    run_id: UUID
    seq: int
    type: str
    payload: dict[str, Any]
    created_at: datetime

    @property
    def event_id(self) -> str:
        """SSE 的 id: 字段, 断线重连时浏览器会用它填 Last-Event-ID。"""

        return f"{self.run_id}:{self.seq}"

    def envelope(self) -> dict[str, Any]:
        # seq 用字符串: DB 是 BIGINT, 直接给 JS number 会丢精度(docs/08 §3.2)。
        return {
            "id": self.event_id,
            "run_id": str(self.run_id),
            "seq": str(self.seq),
            "type": self.type,
            "data": self.payload,
        }


@dataclass(frozen=True)
class RunRecord:
    id: UUID
    conversation_id: UUID
    goal: str
    status: str
    worker_id: str | None
    lease_until: datetime | None
    cancel_requested_at: datetime | None
    budget_tokens: int
    budget_calls: int
    budget_wall_ms: int
    used_tokens: int
    used_calls: int
    next_seq: int
    error: str | None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES

    @property
    def cancel_requested(self) -> bool:
        return self.cancel_requested_at is not None


_RUN_COLUMNS = """
    id, conversation_id, goal, status, worker_id, lease_until, cancel_requested_at,
    budget_tokens, budget_calls, budget_wall_ms, used_tokens, used_calls, next_seq, error
"""


async def ensure_conversation(
    session: AsyncSession,
    *,
    conversation_id: UUID | None = None,
    scope: str = "local_owner",
    demo_session_id: UUID | None = None,
    title: str | None = None,
) -> UUID:
    """复用已有对话或新建一个。demo 作用域必须带 session, 由建表约束保证。"""

    if conversation_id is not None:
        found = (
            await session.execute(
                text("SELECT id FROM conversations WHERE id = :id"),
                {"id": conversation_id},
            )
        ).scalar_one_or_none()
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
) -> RunRecord:
    if not goal.strip():
        raise ValueError("run 目标不能为空")
    run_id = uuid7()
    await session.execute(
        text(
            """
            INSERT INTO agent_runs
                (id, conversation_id, goal, status,
                 budget_tokens, budget_calls, budget_wall_ms)
            VALUES (:id, :conversation_id, :goal, 'queued',
                    :budget_tokens, :budget_calls, :budget_wall_ms)
            """
        ),
        {
            "id": run_id,
            "conversation_id": conversation_id,
            "goal": goal,
            "budget_tokens": budget_tokens,
            "budget_calls": budget_calls,
            "budget_wall_ms": budget_wall_ms,
        },
    )
    run = await get_run(session, run_id)
    if run is None:  # pragma: no cover - 同事务内必然可见
        raise RunNotFoundError(str(run_id))
    return run


async def get_run(session: AsyncSession, run_id: UUID) -> RunRecord | None:
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


async def request_cancel(session: AsyncSession, *, run_id: UUID) -> RunRecord:
    """请求取消。

    还没被 worker 领走就直接落终态; 已经在跑则只打标记, 由 worker 在下一个检查点
    自己收尾——否则会出现"状态已终态但事件还在继续写"的不一致。
    """

    row = (
        (
            await session.execute(
                text(
                    f"""
                    UPDATE agent_runs
                    SET cancel_requested_at = COALESCE(cancel_requested_at, now()),
                        status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
                        finished_at = CASE WHEN status = 'queued' THEN now() ELSE finished_at END,
                        updated_at = now()
                    WHERE id = :run_id
                    RETURNING {_RUN_COLUMNS}
                    """
                ),
                {"run_id": run_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise RunNotFoundError(str(run_id))
    return RunRecord(**row)


async def reap_expired_runs(session: AsyncSession, *, limit: int = 50) -> list[UUID]:
    """回收租约过期的 run, 明确标记为失败。

    普通流式回答不自动重试: 一次已经发出去的模型调用是否计费无法确认, 静默重放
    等于重复计费(docs/08 §3.1)。能自动恢复的只有带 checkpoint 且工具幂等的 Agent run。
    """

    if limit < 1:
        raise ValueError("limit 必须大于 0")
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
                        SELECT id FROM agent_runs
                        WHERE status NOT IN {_TERMINAL_SQL}
                          AND lease_until IS NOT NULL
                          AND lease_until < now()
                        ORDER BY lease_until
                        LIMIT :limit
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id
                    """
                ),
                {"limit": limit},
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
    return [UUID(str(run_id)) for run_id in run_ids]


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
