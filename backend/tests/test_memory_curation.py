"""两套记忆合并之后的时序有效性与策展纪律。

原来这些断言跑在 PostgreSQL 的 `memories` 表上（`tests/test_memory_store.py`）。
表没了，但语义必须原样活下来——它是 ADR-0005 在记忆上的落点，也是两个开源参照物
都没有的那一层：openworker 直接改写记忆，DeepTutor 靠 LLM 重新合并文档。
"""

from datetime import UTC, datetime, timedelta

import pytest
from uuid6 import uuid7

from app.cowork.memory import (
    apply_memory_operation,
    claim_memory_job,
    get_active_successor,
    get_curated_memory,
    list_curated_memories,
    list_dispatchable_memory_jobs,
    retry_or_fail_memory_job,
    schedule_memory_extraction,
    set_memory_pinned,
)
from app.cowork_contracts import MemoryNotFoundError, PinnedMemoryError

pytestmark = pytest.mark.usefixtures("local_cowork_store")

NOW = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)


async def _add(fact: str, *, valid_from: datetime = NOW, pinned: bool = False):
    write = await apply_memory_operation(
        operation="ADD",
        category="preference",
        fact=fact,
        confidence=0.9,
        valid_from=valid_from,
        actor="manual",
        pinned=pinned,
    )
    assert write.memory is not None
    return write.memory


async def test_update_supersedes_instead_of_overwriting() -> None:
    original = await _add("偏好简洁回答")
    later = NOW + timedelta(hours=1)

    updated = await apply_memory_operation(
        operation="UPDATE",
        category="preference",
        fact="偏好详细回答",
        confidence=1.0,
        valid_from=later,
        actor="model",
        target_id=original.id,
    )
    assert updated.applied and updated.current_changed
    assert updated.memory is not None

    historical = await get_curated_memory(original.id)
    assert historical is not None
    # 旧的那条没有被改内容，只是标了"从这一刻起不再成立"，并指向接替它的那条。
    assert historical.content == "偏好简洁回答"
    assert historical.invalid_at == later
    assert historical.superseded_by == updated.memory.id

    assert [item.id for item in await list_curated_memories(active=True)] == [updated.memory.id]
    assert [item.id for item in await list_curated_memories(active=False)] == [original.id]
    assert await get_active_successor(original.id) == updated.memory


async def test_late_arriving_old_fact_becomes_history_without_reversing_current() -> None:
    """模型今天才提炼出上个月的偏好，不能把更新的当前状态顶掉。"""

    current = await _add("正在做智能体评测", valid_from=NOW)
    older = NOW - timedelta(days=1)

    late = await apply_memory_operation(
        operation="UPDATE",
        category="preference",
        fact="正在做多模态检索",
        confidence=0.9,
        valid_from=older,
        actor="model",
        target_id=current.id,
    )
    assert late.applied is True
    assert late.current_changed is False
    assert late.memory is not None
    # 迟到的旧事实以"已失效"形态入库，接替者就是当前那条。
    assert late.memory.invalid_at == NOW
    assert late.memory.superseded_by == current.id
    assert [item.id for item in await list_curated_memories(active=True)] == [current.id]

    stale_delete = await apply_memory_operation(
        operation="DELETE",
        category="preference",
        fact="不再做智能体评测",
        confidence=0.9,
        valid_from=older,
        actor="model",
        target_id=current.id,
    )
    assert stale_delete.applied is False
    assert [item.id for item in await list_curated_memories(active=True)] == [current.id]


async def test_pinned_memory_refuses_automatic_rewrite_and_noop_counts_a_hit() -> None:
    pinned = await _add("永远用表格", pinned=True)

    for operation in ("UPDATE", "DELETE"):
        with pytest.raises(PinnedMemoryError):
            await apply_memory_operation(
                operation=operation,  # type: ignore[arg-type]
                category="preference",
                fact="改成要点列表",
                confidence=1.0,
                valid_from=NOW + timedelta(hours=1),
                actor="model",
                target_id=pinned.id,
            )

    noop = await apply_memory_operation(
        operation="NOOP",
        category="preference",
        fact="永远用表格",
        confidence=1.0,
        valid_from=NOW,
        actor="model",
        target_id=pinned.id,
    )
    assert noop.applied and not noop.current_changed
    assert noop.memory is not None and noop.memory.access_count == 1

    released = await set_memory_pinned(memory_id=pinned.id, pinned=False)
    assert released.pinned is False
    with pytest.raises(MemoryNotFoundError):
        await apply_memory_operation(
            operation="UPDATE",
            category="preference",
            fact="x",
            confidence=1.0,
            valid_from=NOW,
            actor="model",
            target_id=uuid7(),
        )


async def test_delete_marks_invalid_without_a_successor() -> None:
    memory = await _add("临时约定")
    later = NOW + timedelta(hours=2)

    deleted = await apply_memory_operation(
        operation="DELETE",
        category="preference",
        fact="临时约定",
        confidence=1.0,
        valid_from=later,
        actor="manual",
        target_id=memory.id,
    )
    assert deleted.applied and deleted.current_changed
    refreshed = await get_curated_memory(memory.id)
    assert refreshed is not None
    assert refreshed.invalid_at == later
    assert refreshed.superseded_by is None
    # 没有接替者：面板上这条只是"不再成立"，不指向任何新记忆。
    assert await get_active_successor(memory.id) is None
    assert await list_curated_memories(active=True) == []


async def test_extraction_job_is_idempotent_and_recovers_after_a_lost_lease() -> None:
    run_id = uuid7()
    first = await schedule_memory_extraction(
        run_id=run_id,
        conversation_id=uuid7(),
        source_message_id=uuid7(),
        content="以后所有摘要都用表格",
        source_created_at=NOW,
    )
    second = await schedule_memory_extraction(
        run_id=run_id,
        conversation_id=uuid7(),
        source_message_id=uuid7(),
        content="不该覆盖",
        source_created_at=NOW,
    )
    assert first is not None and second is not None
    assert first.id == second.id
    assert second.content == "以后所有摘要都用表格"

    claimed = await claim_memory_job(
        job_id=first.id, worker_id="worker-1", lease_s=30, max_attempts=3
    )
    assert claimed is not None and claimed.attempts == 1
    assert (
        await claim_memory_job(job_id=first.id, worker_id="worker-2", lease_s=30, max_attempts=3)
        is None
    )

    assert (
        await retry_or_fail_memory_job(
            job_id=first.id,
            worker_id="worker-1",
            error="模型超时",
            max_attempts=3,
            retry_delay_s=0,
        )
        == "queued"
    )
    assert await list_dispatchable_memory_jobs(max_attempts=3) == [(first.id, 1)]

    # 重试耗尽的作业必须收敛成 failed，否则会永远挂在"看起来还在跑"的状态里。
    for _ in range(2):
        assert (
            await claim_memory_job(job_id=first.id, worker_id="w", lease_s=30, max_attempts=3)
            is not None
        )
        await retry_or_fail_memory_job(
            job_id=first.id, worker_id="w", error="又失败了", max_attempts=3, retry_delay_s=0
        )
    assert await list_dispatchable_memory_jobs(max_attempts=3) == []
    exhausted = await claim_memory_job(job_id=first.id, worker_id="w", lease_s=30, max_attempts=3)
    assert exhausted is None
