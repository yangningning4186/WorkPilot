from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.platform.demo_sessions import resolve_demo_session
from app.rag.memory.store import (
    PinnedMemoryError,
    apply_memory_operation,
    claim_memory_job,
    complete_memory_job,
    get_memory,
    list_dispatchable_memory_jobs,
    list_memories,
    retry_or_fail_memory_job,
    schedule_memory_extraction,
    search_active_memories,
)
from app.runstore.runs import append_message, create_run, ensure_conversation, finish_run


def _embedding(slot: int) -> list[float]:
    vector = [0.0] * 1024
    vector[slot] = 1.0
    return vector


async def _owner_message(db_session: AsyncSession) -> tuple[object, object]:
    conversation_id = await ensure_conversation(db_session, scope="local_owner")
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="我偏好简洁回答",
        budget_tokens=1000,
        budget_calls=5,
        budget_wall_ms=30_000,
    )
    message_id = await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content="我偏好简洁回答",
        status="completed",
        run_id=run.id,
    )
    assert await finish_run(db_session, run_id=run.id, status="done")
    await db_session.commit()
    return run, message_id


async def test_memory_operations_preserve_history_and_protect_pinned(
    db_session: AsyncSession,
) -> None:
    _, message_id = await _owner_message(db_session)
    now = datetime.now(UTC)

    added = await apply_memory_operation(
        db_session,
        operation="ADD",
        category="preference",
        fact="偏好简洁回答",
        confidence=0.9,
        valid_from=now,
        actor="model",
        source_message_id=message_id,
        embedding=_embedding(1),
        embedding_model="fake-embedding",
        embedding_provider="deterministic_test",
        embedding_revision="memory-test",
        pinned=True,
    )
    assert added.memory is not None

    with pytest.raises(PinnedMemoryError, match="置顶记忆"):
        await apply_memory_operation(
            db_session,
            operation="UPDATE",
            category="preference",
            fact="偏好详细回答",
            confidence=0.8,
            valid_from=now,
            actor="model",
            source_message_id=message_id,
            embedding=_embedding(2),
            embedding_model="fake-embedding",
            embedding_provider="deterministic_test",
            embedding_revision="memory-test",
            target_id=added.memory.id,
        )

    update_time = now + timedelta(seconds=1)
    updated = await apply_memory_operation(
        db_session,
        operation="UPDATE",
        category="preference",
        fact="偏好详细回答",
        confidence=1.0,
        valid_from=update_time,
        actor="manual",
        source_message_id=None,
        embedding=_embedding(2),
        embedding_model="fake-embedding",
        embedding_provider="deterministic_test",
        embedding_revision="memory-test",
        target_id=added.memory.id,
        pinned=True,
    )
    assert updated.memory is not None
    historical = await get_memory(db_session, added.memory.id)
    assert historical is not None
    assert historical.invalid_at is not None
    assert historical.invalid_at == update_time
    assert historical.superseded_by == updated.memory.id

    noop = await apply_memory_operation(
        db_session,
        operation="NOOP",
        category="preference",
        fact="偏好详细回答",
        confidence=1.0,
        valid_from=now,
        actor="model",
        source_message_id=message_id,
        embedding=_embedding(2),
        embedding_model="fake-embedding",
        embedding_provider="deterministic_test",
        embedding_revision="memory-test",
        target_id=updated.memory.id,
    )
    assert noop.memory is not None
    assert noop.memory.access_count == 1

    active = await list_memories(db_session, active=True)
    history = await list_memories(db_session, active=False)
    assert [item.id for item in active] == [updated.memory.id]
    assert [item.id for item in history] == [added.memory.id]


async def test_memory_search_isolated_by_embedding_identity(db_session: AsyncSession) -> None:
    _, message_id = await _owner_message(db_session)
    now = datetime.now(UTC)
    for fact, slot, revision in [
        ("偏好简洁回答", 1, "current"),
        ("旧模型记忆", 2, "old"),
    ]:
        await apply_memory_operation(
            db_session,
            operation="ADD",
            category="preference",
            fact=fact,
            confidence=1.0,
            valid_from=now,
            actor="model",
            source_message_id=message_id,
            embedding=_embedding(slot),
            embedding_model="fake-embedding",
            embedding_provider="deterministic_test",
            embedding_revision=revision,
        )

    hits = await search_active_memories(
        db_session,
        embedding=_embedding(1),
        embedding_model="fake-embedding",
        embedding_provider="deterministic_test",
        embedding_revision="current",
    )

    assert [item.fact for item in hits] == ["偏好简洁回答"]


async def test_out_of_order_model_update_becomes_history_without_reversing_current(
    db_session: AsyncSession,
) -> None:
    _, message_id = await _owner_message(db_session)
    newer_time = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)
    middle_time = datetime(2026, 8, 17, 18, 0, tzinfo=UTC)
    older_time = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    current = await apply_memory_operation(
        db_session,
        operation="ADD",
        category="profile",
        fact="正在做智能体评测",
        confidence=0.95,
        valid_from=newer_time,
        actor="model",
        source_message_id=message_id,
        embedding=_embedding(3),
        embedding_model="fake-embedding",
        embedding_provider="deterministic_test",
        embedding_revision="memory-test",
    )
    assert current.memory is not None

    late_old_job = await apply_memory_operation(
        db_session,
        operation="UPDATE",
        category="profile",
        fact="正在做多模态检索",
        confidence=0.9,
        valid_from=older_time,
        actor="model",
        source_message_id=message_id,
        embedding=_embedding(4),
        embedding_model="fake-embedding",
        embedding_provider="deterministic_test",
        embedding_revision="memory-test",
        target_id=current.memory.id,
    )

    assert late_old_job.applied is True
    assert late_old_job.current_changed is False
    assert late_old_job.memory is not None
    assert late_old_job.memory.invalid_at == newer_time
    assert late_old_job.memory.superseded_by == current.memory.id
    active = await list_memories(db_session, active=True)
    assert [item.id for item in active] == [current.memory.id]

    late_middle_job = await apply_memory_operation(
        db_session,
        operation="UPDATE",
        category="profile",
        fact="正在做 RAG 系统",
        confidence=0.9,
        valid_from=middle_time,
        actor="model",
        source_message_id=message_id,
        embedding=_embedding(5),
        embedding_model="fake-embedding",
        embedding_provider="deterministic_test",
        embedding_revision="memory-test",
        target_id=current.memory.id,
    )
    assert late_middle_job.memory is not None
    assert late_middle_job.memory.invalid_at == newer_time
    assert late_middle_job.memory.superseded_by == current.memory.id
    refreshed_oldest = await get_memory(db_session, late_old_job.memory.id)
    assert refreshed_oldest is not None
    assert refreshed_oldest.invalid_at == middle_time
    assert refreshed_oldest.superseded_by == late_middle_job.memory.id

    stale_delete = await apply_memory_operation(
        db_session,
        operation="DELETE",
        category="profile",
        fact="不再做智能体评测",
        confidence=0.9,
        valid_from=older_time,
        actor="model",
        source_message_id=message_id,
        embedding=None,
        embedding_model=None,
        embedding_provider=None,
        embedding_revision=None,
        target_id=current.memory.id,
    )
    assert stale_delete.applied is False
    assert [item.id for item in await list_memories(db_session, active=True)] == [current.memory.id]


async def test_extraction_job_is_owner_only_idempotent_and_recoverable(
    db_session: AsyncSession,
) -> None:
    owner_run, _ = await _owner_message(db_session)
    first = await schedule_memory_extraction(db_session, run_id=owner_run.id)
    second = await schedule_memory_extraction(db_session, run_id=owner_run.id)
    assert first is not None
    assert second is not None
    assert first.id == second.id

    claimed = await claim_memory_job(
        db_session,
        job_id=first.id,
        worker_id="worker-1",
        lease_s=30,
        max_attempts=3,
    )
    assert claimed is not None
    assert claimed.content == "我偏好简洁回答"
    assert (
        await claim_memory_job(
            db_session,
            job_id=first.id,
            worker_id="worker-2",
            lease_s=30,
            max_attempts=3,
        )
        is None
    )
    assert (
        await retry_or_fail_memory_job(
            db_session,
            job_id=first.id,
            worker_id="worker-1",
            error="暂时失败",
            max_attempts=3,
            retry_delay_s=0,
        )
        == "queued"
    )
    reclaimed = await claim_memory_job(
        db_session,
        job_id=first.id,
        worker_id="worker-2",
        lease_s=30,
        max_attempts=3,
    )
    assert reclaimed is not None
    assert reclaimed.job.attempts == 2
    assert await complete_memory_job(
        db_session,
        job_id=first.id,
        worker_id="worker-2",
        operations=[{"operation": "ADD"}],
    )

    resolved = await resolve_demo_session(db_session, cookie_token=None, ttl_s=300)
    demo_conversation = await ensure_conversation(
        db_session,
        scope="demo",
        demo_session_id=resolved.session.id,
    )
    demo_run = await create_run(
        db_session,
        conversation_id=demo_conversation,
        goal="记住我",
        budget_tokens=1000,
        budget_calls=5,
        budget_wall_ms=30_000,
    )
    await append_message(
        db_session,
        conversation_id=demo_conversation,
        role="user",
        content="记住我",
        status="completed",
        run_id=demo_run.id,
    )
    assert await finish_run(db_session, run_id=demo_run.id, status="done")
    assert await schedule_memory_extraction(db_session, run_id=demo_run.id) is None


async def test_cowork_run_also_schedules_memory_distillation(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, scope="local_owner")
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="以后所有摘要都用表格",
        budget_tokens=1000,
        budget_calls=5,
        budget_wall_ms=30_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content="以后所有摘要都用表格",
        status="completed",
        run_id=run.id,
    )
    assert await finish_run(db_session, run_id=run.id, status="done")

    assert await schedule_memory_extraction(db_session, run_id=run.id) is not None


async def test_sqlite_cowork_source_schedules_and_claims_memory_job(
    db_session: AsyncSession,
) -> None:
    run_id = uuid7()
    message_id = uuid7()
    conversation_id = uuid7()
    created_at = datetime.now(UTC)

    job = await schedule_memory_extraction(
        db_session,
        run_id=run_id,
        local_source_message_id=message_id,
        local_conversation_id=conversation_id,
        local_content="以后所有摘要都使用表格",
        local_created_at=created_at,
    )
    assert job is not None and job.source_is_local is True
    source = await claim_memory_job(
        db_session,
        job_id=job.id,
        worker_id="local-memory",
        lease_s=30,
        max_attempts=3,
    )

    assert source is not None
    assert source.job.run_id == run_id
    assert source.job.source_message_id == message_id
    assert source.conversation_id == conversation_id
    assert source.content == "以后所有摘要都使用表格"


async def test_dispatcher_recovers_expired_jobs_and_finalizes_exhausted_ones(
    db_session: AsyncSession,
) -> None:
    first_run, _ = await _owner_message(db_session)
    first = await schedule_memory_extraction(db_session, run_id=first_run.id)
    assert first is not None
    assert (
        await claim_memory_job(
            db_session,
            job_id=first.id,
            worker_id="dead-worker",
            lease_s=30,
            max_attempts=3,
        )
        is not None
    )
    await db_session.execute(
        text("UPDATE memory_extraction_jobs SET lease_until = now() - interval '1 second'")
    )
    dispatchable = await list_dispatchable_memory_jobs(db_session, max_attempts=3)
    assert dispatchable == [(first.id, 1)]

    await db_session.execute(
        text("UPDATE memory_extraction_jobs SET attempts = 3 WHERE id = :id"),
        {"id": first.id},
    )
    assert await list_dispatchable_memory_jobs(db_session, max_attempts=3) == []
    status = (
        await db_session.execute(
            text("SELECT status FROM memory_extraction_jobs WHERE id = :id"),
            {"id": first.id},
        )
    ).scalar_one()
    assert status == "failed"
