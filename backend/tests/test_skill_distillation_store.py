import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.services.runs import create_run, ensure_conversation, finish_run
from app.skills.distillation_store import (
    claim_skill_job,
    schedule_skill_distillation,
    upsert_skill_candidate,
)

pytestmark = pytest.mark.integration


async def test_skill_job_is_idempotent_and_uses_successful_tool_names_only(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, scope="local_owner")
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="整理报告并生成摘要",
        budget_tokens=10_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    state = {
        "final_message": "已生成摘要。",
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "ok-call",
                        "type": "function",
                        "function": {"name": "read_text_file", "arguments": "{}"},
                    },
                    {
                        "id": "failed-call",
                        "type": "function",
                        "function": {"name": "run_shell", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "ok-call", "content": '{"ok":true}'},
            {"role": "tool", "tool_call_id": "failed-call", "content": '{"ok":false}'},
        ],
    }
    await db_session.execute(
        text(
            """
            INSERT INTO agent_checkpoints (run_id, checkpoint_id, state)
            VALUES (:run_id, :checkpoint_id, CAST(:state AS jsonb))
            """
        ),
        {"run_id": run.id, "checkpoint_id": "0001", "state": json.dumps(state)},
    )
    assert await finish_run(db_session, run_id=run.id, status="done")

    first = await schedule_skill_distillation(db_session, run_id=run.id)
    second = await schedule_skill_distillation(db_session, run_id=run.id)
    assert first is not None and second is not None and first.id == second.id
    source = await claim_skill_job(
        db_session,
        job_id=first.id,
        worker_id="skill-test",
        lease_s=30,
        max_attempts=3,
    )
    assert source is not None
    assert source.successful_tools == ["read_text_file"]

    candidate = await upsert_skill_candidate(
        db_session,
        run_id=run.id,
        capability_key="summarize-report",
        suggested_name="learned-summarize-report",
        description="整理报告",
        skill_md="---\nname: learned-summarize-report\ndescription: 整理报告\n---\n\n1. 读取报告\n",
        tools=["read_text_file"],
        confidence=0.9,
    )
    repeated = await upsert_skill_candidate(
        db_session,
        run_id=run.id,
        capability_key="summarize-report",
        suggested_name="learned-summarize-report",
        description="整理报告",
        skill_md=candidate.skill_md,
        tools=["read_text_file"],
        confidence=0.9,
    )
    assert repeated.id == candidate.id
    assert repeated.evidence_count == 1


async def test_sqlite_cowork_source_can_distill_and_record_evidence(
    db_session: AsyncSession,
) -> None:
    run_id = uuid7()
    job = await schedule_skill_distillation(
        db_session,
        run_id=run_id,
        local_goal="整理预算并输出报告",
        local_final_message="报告已生成。",
        local_successful_tools=["read_text_file", "create_artifact"],
    )
    assert job is not None and job.source_is_local is True
    source = await claim_skill_job(
        db_session,
        job_id=job.id,
        worker_id="local-skill",
        lease_s=30,
        max_attempts=3,
    )
    assert source is not None
    assert source.goal == "整理预算并输出报告"
    assert source.successful_tools == ["read_text_file", "create_artifact"]

    candidate = await upsert_skill_candidate(
        db_session,
        run_id=run_id,
        capability_key="local-budget-report",
        suggested_name="learned-local-budget-report",
        description="整理本地预算",
        skill_md="---\nname: learned-local-budget-report\ndescription: 整理预算\n---\n",
        tools=source.successful_tools,
        confidence=0.9,
    )
    assert candidate.last_run_id == run_id
    assert candidate.evidence_count == 1
