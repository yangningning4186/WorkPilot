from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.persistence import load_latest_checkpoint
from app.agent.review_graph import (
    ReviewTools,
    initialize_review_state,
    run_readonly_review,
)
from app.agent.state import ReviewCard, ReviewDocument, ReviewGroup
from app.services.runs import create_run, ensure_conversation, list_events

pytestmark = pytest.mark.integration


class FakeReviewTools(ReviewTools):
    def __init__(self, *, fail_compare: bool = False) -> None:
        self.fail_compare = fail_compare
        self.calls: list[str] = []

    async def list_documents(self, document_ids: list[str]) -> list[ReviewDocument]:
        self.calls.append("list_documents")
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
        self.calls.append(f"extract_card:{document['document_id']}")
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
        self.calls.append("group_cards")
        return [{"name": "memory", "document_ids": [item["document_id"] for item in cards]}]

    async def compare_documents(
        self, cards: list[ReviewCard], groups: list[ReviewGroup]
    ) -> str:
        self.calls.append("compare_documents")
        if self.fail_compare:
            raise RuntimeError("模拟 worker 在比较步骤失败")
        return f"比较 {len(cards)} 篇、{len(groups)} 组"

    async def generate_review(
        self,
        *,
        goal: str,
        cards: list[ReviewCard],
        groups: list[ReviewGroup],
        comparison: str,
    ) -> str:
        self.calls.append("generate_review")
        return f"# {goal}\n\n{comparison}\n\n共 {len(cards)} 篇。"


async def _review_run(session: AsyncSession) -> tuple[UUID, list[UUID]]:
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
    document_ids = [uuid4(), uuid4()]
    await initialize_review_state(
        session,
        run_id=run.id,
        document_ids=document_ids,
        output_path="reviews/memory.md",
    )
    return run.id, document_ids


async def test_readonly_review_emits_preview_and_waits_for_human(
    db_session: AsyncSession,
) -> None:
    run_id, _ = await _review_run(db_session)
    tools = FakeReviewTools()
    state = await run_readonly_review(db_session, run_id=run_id, tools=tools)

    assert state["cursor"] == 5
    assert state["status"] == "waiting_human"
    assert state["interrupt"] is not None
    assert state["interrupt"]["kind"] == "write_confirm"
    assert state["draft"].startswith("# 比较记忆方法")
    assert tools.calls.count("list_documents") == 1
    assert sum(call.startswith("extract_card:") for call in tools.calls) == 2

    events = await list_events(db_session, run_id=run_id)
    assert events[0].type == "plan"
    assert [event.type for event in events[-2:]] == ["artifact", "interrupt"]
    assert events[-1].payload["resume_token"] == state["interrupt"]["resume_token"]


async def test_recovery_resumes_from_last_completed_node(
    db_session: AsyncSession,
) -> None:
    run_id, _ = await _review_run(db_session)
    failing = FakeReviewTools(fail_compare=True)
    with pytest.raises(RuntimeError, match="比较步骤失败"):
        await run_readonly_review(db_session, run_id=run_id, tools=failing)

    failed_checkpoint = await load_latest_checkpoint(db_session, run_id=run_id)
    assert failed_checkpoint is not None
    assert failed_checkpoint.state["cursor"] == 3
    assert failed_checkpoint.state["plan"][3]["status"] == "failed"

    resumed = FakeReviewTools()
    state = await run_readonly_review(db_session, run_id=run_id, tools=resumed)
    assert state["status"] == "waiting_human"
    # 已有 checkpoint 的前三步不会重跑，只继续 compare + preview。
    assert resumed.calls == ["compare_documents", "generate_review"]

    attempts = (
        await db_session.execute(
            text(
                """
                SELECT node, attempt_no, status FROM agent_attempts
                WHERE run_id = :run_id AND node = 'compare_documents'
                ORDER BY attempt_no
                """
            ),
            {"run_id": run_id},
        )
    ).all()
    assert attempts == [
        ("compare_documents", 1, "failed"),
        ("compare_documents", 2, "ok"),
    ]
