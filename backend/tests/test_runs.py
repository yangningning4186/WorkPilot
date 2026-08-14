import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.services.runs import (
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
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

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


async def test_expired_lease_allows_reclaim_but_live_lease_does_not(
    db_session: AsyncSession,
) -> None:
    run = await _new_run(db_session)
    assert await claim_run(db_session, run_id=run.id, worker_id="worker-a", lease_s=60)
    assert await claim_run(db_session, run_id=run.id, worker_id="worker-b", lease_s=60) is None

    await db_session.execute(
        text("UPDATE agent_runs SET lease_until = now() - interval '1 second' WHERE id = :id"),
        {"id": run.id},
    )
    assert await claim_run(db_session, run_id=run.id, worker_id="worker-b", lease_s=60)


async def test_watchdog_fails_expired_runs_and_marks_message(db_session: AsyncSession) -> None:
    """普通流式回答不自动重试: 是否已计费无法确认, 静默重放等于重复计费。"""

    run = await _new_run(db_session)
    await claim_run(db_session, run_id=run.id, worker_id="worker-a", lease_s=60)
    message_id = await append_message(
        db_session,
        conversation_id=run.conversation_id,
        role="assistant",
        status="streaming",
        run_id=run.id,
    )
    await db_session.execute(
        text("UPDATE agent_runs SET lease_until = now() - interval '1 second' WHERE id = :id"),
        {"id": run.id},
    )

    reaped = await reap_expired_runs(db_session)
    assert reaped == [run.id]

    refreshed = await get_run(db_session, run.id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error is not None

    events = await list_events(db_session, run_id=run.id)
    assert [event.type for event in events] == ["error"]
    assert events[0].payload["code"] == "worker_lease_expired"
    assert events[0].payload["retryable"] is True

    status = (
        await db_session.execute(
            text("SELECT status FROM messages WHERE id = :id"), {"id": message_id}
        )
    ).scalar_one()
    assert status == "failed"


async def test_watchdog_ignores_runs_with_live_lease(db_session: AsyncSession) -> None:
    run = await _new_run(db_session)
    await claim_run(db_session, run_id=run.id, worker_id="worker-a", lease_s=600)
    assert await reap_expired_runs(db_session) == []


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


async def test_finish_run_requires_matching_worker(db_session: AsyncSession) -> None:
    run = await _new_run(db_session)
    await claim_run(db_session, run_id=run.id, worker_id="worker-a", lease_s=60)

    assert not await finish_run(db_session, run_id=run.id, status="done", worker_id="worker-b")
    assert await finish_run(db_session, run_id=run.id, status="done", worker_id="worker-a")

    refreshed = await get_run(db_session, run.id)
    assert refreshed is not None
    assert refreshed.status == "done"
    assert refreshed.is_terminal


async def test_messages_get_sequential_seq_per_conversation(db_session: AsyncSession) -> None:
    conversation_id = await ensure_conversation(db_session)
    await append_message(db_session, conversation_id=conversation_id, role="user", content="一")
    await append_message(
        db_session, conversation_id=conversation_id, role="assistant", content="二"
    )

    seqs = list(
        (
            await db_session.execute(
                text("SELECT seq FROM messages WHERE conversation_id = :id ORDER BY seq"),
                {"id": conversation_id},
            )
        )
        .scalars()
        .all()
    )
    assert seqs == [1, 2]
