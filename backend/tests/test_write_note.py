from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.budget import BudgetMeter
from app.agent.review_graph import ReviewTools, initialize_review_state, run_readonly_review
from app.agent.state import ReviewCard, ReviewDocument, ReviewGroup
from app.agent.write_note import (
    acquire_invocation,
    resolve_note_path,
    resume_review_after_human,
    review_resume_token,
    write_note,
)
from app.services.runs import create_run, ensure_conversation, get_run, list_events
from tests.fakes import review_budget

pytestmark = pytest.mark.integration


class ReadyReviewTools(ReviewTools):
    async def list_documents(self, document_ids: list[str]) -> list[ReviewDocument]:
        return [
            {
                "document_id": item,
                "version_id": str(uuid4()),
                "title": f"论文-{index}",
                "source_uri": f"file:///paper-{index}.pdf",
            }
            for index, item in enumerate(document_ids)
        ]

    async def extract_card(self, document: ReviewDocument) -> ReviewCard:
        return {
            "document_id": document["document_id"],
            "title": document["title"],
            "core_problem": "问题",
            "method_family": "memory",
            "method": "方法",
            "findings": ["结论"],
            "limitations": ["局限"],
            "evidence_quotes": ["证据"],
        }

    async def group_cards(self, cards: list[ReviewCard]) -> list[ReviewGroup]:
        return [{"name": "memory", "document_ids": [item["document_id"] for item in cards]}]

    async def compare_documents(
        self, cards: list[ReviewCard], groups: list[ReviewGroup]
    ) -> str:
        return "两篇论文采用同类方法，但实验结论和局限不同。"

    async def generate_review(
        self,
        *,
        goal: str,
        cards: list[ReviewCard],
        groups: list[ReviewGroup],
        comparison: str,
    ) -> str:
        return f"# {goal}\n\n{comparison}\n"


async def _waiting_review(session: AsyncSession) -> tuple[UUID, str]:
    conversation_id = await ensure_conversation(session)
    run = await create_run(
        session,
        conversation_id=conversation_id,
        goal="比较记忆方法",
        budget_tokens=10_000,
        budget_calls=20,
        budget_wall_ms=60_000,
        workflow_type="literature_review",
    )
    await initialize_review_state(
        session,
        run_id=run.id,
        document_ids=[uuid4(), uuid4()],
        output_path="reviews/memory.md",
    )
    state = await run_readonly_review(
        session, run_id=run.id, tools=ReadyReviewTools(), meter=BudgetMeter(review_budget())
    )
    assert state["status"] == "waiting_human"
    return run.id, state["draft"]


async def test_write_note_recovers_rename_before_database_settlement_without_rewrite(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    run_id, draft = await _waiting_review(db_session)
    step_id = UUID(
        str(
            (
                await db_session.execute(
                text(
                    "SELECT id FROM agent_plan_steps WHERE run_id = :run_id AND step_idx = 5"
                ),
                {"run_id": run_id},
            )
            ).scalar_one()
        )
    )

    def crash_after_replace() -> None:
        raise RuntimeError("模拟 rename 后进程退出")

    with pytest.raises(RuntimeError, match="模拟 rename"):
        await write_note(
            db_session,
            run_id=run_id,
            plan_step_id=step_id,
            output_root=tmp_path,
            output_path="reviews/memory.md",
            content=draft,
            worker_id="worker-a",
            after_replace=crash_after_replace,
        )
    target = tmp_path / "reviews/memory.md"
    before = target.stat()

    recovered = await write_note(
        db_session,
        run_id=run_id,
        plan_step_id=step_id,
        output_root=tmp_path,
        output_path="reviews/memory.md",
        content=draft,
        worker_id="worker-b",
    )
    after = target.stat()
    assert recovered.reused is True
    assert target.read_text() == draft
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


async def test_expired_invocation_is_reclaimed_with_cas(
    db_session: AsyncSession,
) -> None:
    run_id, _ = await _waiting_review(db_session)
    step_id = UUID(
        str(
            (
                await db_session.execute(
                text(
                    "SELECT id FROM agent_plan_steps WHERE run_id = :run_id AND step_idx = 5"
                ),
                {"run_id": run_id},
            )
            ).scalar_one()
        )
    )
    args = {"output_path": "reviews/memory.md", "content_sha256": "abc"}
    first = await acquire_invocation(
        db_session,
        run_id=run_id,
        plan_step_id=step_id,
        tool_name="write_note",
        args=args,
        worker_id="worker-a",
        lease_s=30,
    )
    await db_session.commit()
    await db_session.execute(
        text(
            "UPDATE tool_invocations SET lease_until = now() - interval '1 second' "
            "WHERE idempotency_key = :key"
        ),
        {"key": first.idempotency_key},
    )
    second = await acquire_invocation(
        db_session,
        run_id=run_id,
        plan_step_id=step_id,
        tool_name="write_note",
        args=args,
        worker_id="worker-b",
        lease_s=30,
    )
    assert second.acquired is True
    owner, retry_count = (
        await db_session.execute(
            text(
                "SELECT lease_owner, retry_count FROM tool_invocations "
                "WHERE idempotency_key = :key"
            ),
            {"key": first.idempotency_key},
        )
    ).one()
    assert (owner, retry_count) == ("worker-b", 1)


async def test_hitl_approval_writes_once_and_duplicate_resume_is_noop(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    run_id, draft = await _waiting_review(db_session)
    token = review_resume_token(run_id)
    completed = await resume_review_after_human(
        db_session,
        run_id=run_id,
        resume_token=token,
        approved=True,
        output_root=tmp_path,
        worker_id="api-a",
    )
    target = tmp_path / "reviews/memory.md"
    before = target.stat()
    repeated = await resume_review_after_human(
        db_session,
        run_id=run_id,
        resume_token=token,
        approved=True,
        output_root=tmp_path,
        worker_id="api-b",
    )

    assert completed["status"] == repeated["status"] == "done"
    assert target.read_text() == draft
    after = target.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert (await get_run(db_session, run_id)).status == "done"  # type: ignore[union-attr]
    events = await list_events(db_session, run_id=run_id)
    assert [event.type for event in events].count("run.done") == 1
    assert [event.type for event in events].count("interrupt") == 1


async def test_hitl_rejection_never_writes_and_path_traversal_is_rejected(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    run_id, _ = await _waiting_review(db_session)
    completed = await resume_review_after_human(
        db_session,
        run_id=run_id,
        resume_token=review_resume_token(run_id),
        approved=False,
        output_root=tmp_path,
        worker_id="api-a",
    )
    assert completed["status"] == "done"
    assert completed["plan"][5]["status"] == "skipped"
    assert not (tmp_path / "reviews/memory.md").exists()

    with pytest.raises(ValueError, match="相对路径"):
        resolve_note_path(tmp_path, "../outside.md")
    with pytest.raises(ValueError, match=r"\.md"):
        resolve_note_path(tmp_path, "reviews/note.txt")
