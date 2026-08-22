import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import DbSession as AsyncSession
from app.core.db import session_factory
from app.runstore.runs import (
    append_events,
    append_message,
    claim_run,
    create_run,
    ensure_conversation,
    finish_run,
    get_run,
    list_events,
    reap_expired_runs,
    renew_lease,
    request_cancel,
)
from tests.conftest import iso_ago

pytestmark = pytest.mark.integration


async def _new_run(session: AsyncSession, goal: str = "什么是 RRF?"):
    conversation_id = await ensure_conversation(session)
    return await create_run(
        session,
        conversation_id=conversation_id,
        goal=goal,
        budget_tokens=1000,
        budget_calls=5,
        budget_wall_ms=60_000,
    )


async def test_events_get_monotonic_seq_and_replay_from_cursor(db_session: AsyncSession) -> None:
    run = await _new_run(db_session)

    first = await append_events(
        db_session,
        run_id=run.id,
        events=[("message.start", {"message_id": "m1"}), ("message.delta", {"text": "甲"})],
    )
    second = await append_events(
        db_session, run_id=run.id, events=[("message.delta", {"text": "乙"})]
    )

    assert [event.seq for event in first] == [1, 2]
    assert [event.seq for event in second] == [3]
    assert first[0].event_id == f"{run.id}:1"
    # seq 以字符串出信封, 避免 JS number 精度问题。
    assert first[0].envelope()["seq"] == "1"
    assert first[0].envelope()["data"] == {"message_id": "m1"}

    replayed = await list_events(db_session, run_id=run.id, after_seq=2)
    assert [event.seq for event in replayed] == [3]
    assert replayed[0].payload == {"text": "乙"}

    refreshed = await get_run(db_session, run.id)
    assert refreshed is not None
    assert refreshed.next_seq == 4


async def test_concurrent_claims_cannot_double_run(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """条件 UPDATE 抢占是防双跑的主防线, 双跑意味着一次回答计费两次。"""

    run = await _new_run(db_session)
    await db_session.commit()
    factory = session_factory

    async def claim(worker_id: str):
        async with factory() as session:
            claimed = await claim_run(session, run_id=run.id, worker_id=worker_id, lease_s=60)
            await session.commit()
            return claimed

    results = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    assert sum(result is not None for result in results) == 1


async def test_lease_renewal_fails_after_takeover(db_session: AsyncSession) -> None:
    run = await _new_run(db_session)
    claimed = await claim_run(db_session, run_id=run.id, worker_id="worker-a", lease_s=60)
    assert claimed is not None

    assert await renew_lease(db_session, run_id=run.id, worker_id="worker-a", lease_s=60)
    # 别的 worker 不能续别人的租约。
    assert await renew_lease(db_session, run_id=run.id, worker_id="worker-b", lease_s=60) is None


async def test_an_expired_lease_is_reclaimed_through_the_watchdog_not_by_stealing(
    db_session: AsyncSession, store_sql,
) -> None:
    """租约过期不等于可以直接抢。

    PostgreSQL 版的 `claim_run` 允许第二个 worker 直接偷走过期租约。那条路绕开了
    `recovery_count`——ADR-0007 用它挡住"稳定把 worker 拖垮的 run 被无限重投"。
    SQLite 版只认 `queued`，于是回收必须经过 watchdog，重投次数也就必然被计数。
    """

    run = await _new_run(db_session)
    assert await claim_run(db_session, run_id=run.id, worker_id="worker-a", lease_s=60)
    assert await claim_run(db_session, run_id=run.id, worker_id="worker-b", lease_s=60) is None

    store_sql(
        "UPDATE agent_runs SET lease_until = ? WHERE id = ?",
        (iso_ago(1), str(run.id)),
    )
    # 租约过期了，但没经过 watchdog 之前谁也抢不到。
    assert await claim_run(db_session, run_id=run.id, worker_id="worker-b", lease_s=60) is None

    # 这个 run 没有 checkpoint，watchdog 把它判死而不是重投——重投一个不知道跑到
    # 哪一步的 run，副作用会从头再来一遍。带 checkpoint 的恢复见下面那条用例。
    reaped = await reap_expired_runs(db_session)
    assert reaped.failed == [run.id]
    assert await claim_run(db_session, run_id=run.id, worker_id="worker-b", lease_s=60) is None


async def test_watchdog_fails_expired_runs_and_marks_message(db_session: AsyncSession, store_sql, message_status) -> None:
    """普通流式回答不自动重试: 是否已计费无法确认, 静默重放等于重复计费。"""

    run = await _new_run(db_session)
    await claim_run(db_session, run_id=run.id, worker_id="worker-a", lease_s=60)
    await append_message(
        db_session,
        conversation_id=run.conversation_id,
        role="assistant",
        status="streaming",
        run_id=run.id,
    )
    store_sql(
        "UPDATE agent_runs SET lease_until = ? WHERE id = ?",
        (iso_ago(1), str(run.id)),
    )

    reaped = await reap_expired_runs(db_session)
    # 普通回答不可自动恢复: 已发出的模型调用是否计费无法确认。
    assert reaped.failed == [run.id]
    assert reaped.recovered_cowork == []

    refreshed = await get_run(db_session, run.id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error is not None

    events = await list_events(db_session, run_id=run.id)
    assert [event.type for event in events] == ["error"]
    assert events[0].payload["code"] == "worker_lease_expired"
    assert events[0].payload["retryable"] is True

    status = (
        await message_status(run.conversation_id, run.id, "assistant")
    )[0]
    assert status == "failed"


async def test_watchdog_ignores_runs_with_live_lease(db_session: AsyncSession) -> None:
    run = await _new_run(db_session)
    await claim_run(db_session, run_id=run.id, worker_id="worker-a", lease_s=600)
    reaped = await reap_expired_runs(db_session)
    assert reaped.failed == [] and reaped.recovered_cowork == []


async def test_cancel_is_immediate_only_before_pickup(db_session: AsyncSession) -> None:
    queued = await _new_run(db_session)
    cancelled = await request_cancel(db_session, run_id=queued.id)
    assert cancelled.status == "cancelled"
    # 已取消的 run 不允许再被 worker 领走。
    assert await claim_run(db_session, run_id=queued.id, worker_id="worker-a", lease_s=60) is None

    running = await _new_run(db_session)
    await claim_run(db_session, run_id=running.id, worker_id="worker-a", lease_s=60)
    requested = await request_cancel(db_session, run_id=running.id)
    # 执行中只打标记: 直接落终态会出现"终态之后仍在写事件"的不一致。
    assert requested.status == "executing"
    assert requested.cancel_requested is True


async def test_watchdog_finalizes_cancelled_run_after_worker_disappears(
    db_session: AsyncSession, store_sql,
) -> None:
    run = await _new_run(db_session)
    await claim_run(db_session, run_id=run.id, worker_id="worker-a", lease_s=60)
    requested = await request_cancel(db_session, run_id=run.id)
    assert requested.status == "executing"
    store_sql(
        "UPDATE agent_runs SET lease_until = ? WHERE id = ?",
        (iso_ago(1), str(run.id)),
    )

    reaped = await reap_expired_runs(db_session)

    assert reaped.cancelled == [run.id]
    assert reaped.recovered_cowork == []
    refreshed = await get_run(db_session, run.id)
    assert refreshed is not None
    assert refreshed.status == "cancelled"
    events = await list_events(db_session, run_id=run.id)
    assert [event.type for event in events] == ["error", "run.done"]


async def test_finish_run_requires_matching_worker(db_session: AsyncSession) -> None:
    run = await _new_run(db_session)
    await claim_run(db_session, run_id=run.id, worker_id="worker-a", lease_s=60)

    assert not await finish_run(db_session, run_id=run.id, status="done", worker_id="worker-b")
    assert await finish_run(db_session, run_id=run.id, status="done", worker_id="worker-a")

    refreshed = await get_run(db_session, run.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.is_terminal


async def test_messages_get_sequential_seq_per_conversation(db_session: AsyncSession, store_sql) -> None:
    conversation_id = await ensure_conversation(db_session)
    await append_message(db_session, conversation_id=conversation_id, role="user", content="一")
    await append_message(
        db_session, conversation_id=conversation_id, role="assistant", content="二"
    )

    seqs = [
        row["seq"]
        for row in store_sql(
            "SELECT seq FROM conversation_message_index WHERE conversation_id = ? ORDER BY seq",
            (str(conversation_id),),
        )
    ]
    assert seqs == [1, 2]


async def _expired_run(
    session: AsyncSession, store_sql, *, with_checkpoint: bool = True
) -> UUID:
    """一个租约已过期、worker 消失了的 Cowork run。

    checkpoint 用裸 SQL 插，不走 `initialize_cowork_state`：watchdog 判定"可不可恢复"
    只看 `agent_checkpoints` 里有没有行，不读 state 的内容。让这个 fixture 去组装一整套
    工具注册表，只会让它跟着 Cowork 运行时一起漂。
    """
    conversation_id = await ensure_conversation(session)
    run = await create_run(
        session,
        conversation_id=conversation_id,
        goal="比较记忆方法",
        budget_tokens=10_000,
        budget_calls=20,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    if with_checkpoint:
        store_sql(
            """INSERT INTO agent_checkpoints
                   (run_id, checkpoint_id, parent_id, state, created_at)
               VALUES (?, ?, NULL, '{}', datetime('now'))""",
            (str(run.id), str(uuid4())),
        )
    await claim_run(session, run_id=run.id, worker_id="worker-a", lease_s=60)
    store_sql(
        "UPDATE agent_runs SET lease_until = ? WHERE id = ?",
        (iso_ago(1), str(run.id)),
    )
    return run.id


async def test_watchdog_recovers_a_checkpointed_run_instead_of_failing_it(
    db_session: AsyncSession, store_sql,
) -> None:
    """带 checkpoint 的 run 被 SIGKILL 后应重新入队，而不是判死。"""

    run_id = await _expired_run(db_session, store_sql)

    reaped = await reap_expired_runs(db_session)
    assert reaped.failed == []
    assert reaped.recovered_cowork == [(run_id, 1)]

    refreshed = await get_run(db_session, run_id)
    assert refreshed is not None
    # 关键: 必须退回 queued 而不是只清租约, 否则 claim_run 的条件谁都命中不了。
    assert refreshed.status == "queued"
    assert refreshed.lease_until is None

    reclaimed = await claim_run(db_session, run_id=run_id, worker_id="worker-b", lease_s=60)
    assert reclaimed is not None
    assert reclaimed.worker_id == "worker-b"


async def test_watchdog_fails_a_run_without_checkpoint(
    db_session: AsyncSession, store_sql,
) -> None:
    """没有 checkpoint 就没有可恢复的进度，重跑等于从头再烧一遍预算。"""

    run_id = await _expired_run(db_session, store_sql, with_checkpoint=False)

    reaped = await reap_expired_runs(db_session)
    assert reaped.recovered_cowork == []
    assert reaped.failed == [run_id]


async def test_watchdog_stops_recovering_after_the_cap(
    db_session: AsyncSession, store_sql,
) -> None:
    """稳定把 worker 拖垮的 run 必须停下来交给人，不能无限重投。"""

    run_id = await _expired_run(db_session, store_sql)

    for attempt in (1, 2):
        reaped = await reap_expired_runs(db_session, max_recovery=2)
        assert reaped.recovered_cowork == [(run_id, attempt)]
        await claim_run(db_session, run_id=run_id, worker_id="worker-a", lease_s=60)
        store_sql(
            "UPDATE agent_runs SET lease_until = ? WHERE id = ?",
            (iso_ago(1), str(run_id)),
        )

    exhausted = await reap_expired_runs(db_session, max_recovery=2)
    assert exhausted.recovered_cowork == []
    assert exhausted.failed == [run_id]
