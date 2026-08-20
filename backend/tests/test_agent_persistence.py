from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.agent_core.state import PlanStepState, json_state
from app.rag.review.state import ReviewState
from app.runstore.checkpoints import ensure_plan, load_latest_checkpoint, save_checkpoint
from app.runstore.runs import create_run, ensure_conversation, get_run

pytestmark = pytest.mark.integration


def _plan() -> list[PlanStepState]:
    return [
        {
            "id": str(uuid7()),
            "idx": 0,
            "description": "筛选文档",
            "tool": "list_documents",
            "depends_on": [],
            "status": "pending",
        },
        {
            "id": str(uuid7()),
            "idx": 1,
            "description": "抽取卡片",
            "tool": "extract_card",
            "depends_on": [0],
            "status": "pending",
        },
    ]


def _state(run_id: UUID, conversation_id: UUID, plan: list[PlanStepState]) -> ReviewState:
    return {
        "schema_version": "literature-review.v1",
        "run_id": str(run_id),
        "conversation_id": str(conversation_id),
        "goal": "比较两篇论文",
        "document_ids": [],
        "plan": plan,
        "cursor": 0,
        "documents": [],
        "cards": [],
        "groups": [],
        "comparison": "",
        "draft": "",
        "output_path": None,
        "artifacts": {},
        "budget": {
            "max_tokens": 1000,
            "used_tokens": 0,
            "max_calls": 10,
            "used_calls": 0,
            "max_wall_ms": 60_000,
            "started_at_ms": 0,
        },
        "interrupt": None,
        "status": "executing",
        "error": None,
    }


async def test_literature_review_run_persists_plan_and_checkpoint(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session)
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="比较两篇论文",
        budget_tokens=1000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="literature_review",
    )
    assert run.workflow_type == "literature_review"
    assert (await get_run(db_session, run.id)).workflow_type == "literature_review"  # type: ignore[union-attr]

    plan = _plan()
    await ensure_plan(db_session, run_id=run.id, steps=plan)
    first = await save_checkpoint(
        db_session,
        run_id=run.id,
        state=_state(run.id, conversation_id, plan),
        parent_id=None,
    )
    next_state = _state(run.id, conversation_id, plan)
    next_state["cursor"] = 1
    second = await save_checkpoint(
        db_session,
        run_id=run.id,
        state=next_state,
        parent_id=first.checkpoint_id,
    )

    latest = await load_latest_checkpoint(db_session, run_id=run.id)
    assert latest is not None
    assert latest.checkpoint_id == second.checkpoint_id
    assert latest.parent_id == first.checkpoint_id
    assert latest.state["cursor"] == 1

    table_count = (
        await db_session.execute(
            text(
                """
                SELECT count(*) FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = ANY(:table_names)
                """
            ),
            {
                "table_names": [
                    "agent_plan_steps",
                    "agent_attempts",
                    "tool_invocations",
                    "agent_checkpoints",
                ]
            },
        )
    ).scalar_one()
    assert table_count == 4


async def test_workflow_type_and_plan_identity_fail_closed(db_session: AsyncSession) -> None:
    conversation_id = await ensure_conversation(db_session)
    with pytest.raises(ValueError, match="workflow_type"):
        await create_run(
            db_session,
            conversation_id=conversation_id,
            goal="错误 workflow",
            budget_tokens=1,
            budget_calls=1,
            budget_wall_ms=1,
            workflow_type="general_agent",  # type: ignore[arg-type]
        )

    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="固定综述",
        budget_tokens=1,
        budget_calls=1,
        budget_wall_ms=1,
        workflow_type="literature_review",
    )
    plan = _plan()
    await ensure_plan(db_session, run_id=run.id, steps=plan)
    drifted = [dict(step) for step in plan]
    drifted[0]["description"] = "偷偷换步骤"
    with pytest.raises(ValueError, match="漂移"):
        await ensure_plan(db_session, run_id=run.id, steps=drifted)  # type: ignore[arg-type]


def test_agent_state_rejects_non_json_runtime_objects() -> None:
    plan = _plan()
    state = _state(uuid4(), uuid4(), plan)
    state["artifacts"]["bad"] = uuid4()  # type: ignore[assignment]
    with pytest.raises(TypeError):
        json_state(state)
