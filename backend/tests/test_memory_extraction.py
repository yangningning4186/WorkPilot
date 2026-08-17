import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.core.config import get_settings
from app.llm.gateway import ModelGateway
from app.memory.extraction import (
    MemoryExtractionError,
    parse_memory_candidates,
    parse_memory_decision,
    process_memory_job_source,
)
from app.memory.store import (
    MemoryJobSource,
    claim_memory_job,
    list_memories,
    schedule_memory_extraction,
)
from app.services.runs import append_message, create_run, ensure_conversation, finish_run
from app.worker.memory_run import memory_extraction_job
from tests.fakes import DeterministicProvider


async def _owner_job(db_session: AsyncSession, content: str) -> MemoryJobSource:
    conversation_id = await ensure_conversation(db_session, scope="local_owner")
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal=content,
        budget_tokens=1000,
        budget_calls=5,
        budget_wall_ms=30_000,
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=content,
        status="completed",
        run_id=run.id,
    )
    assert await finish_run(db_session, run_id=run.id, status="done")
    job = await schedule_memory_extraction(db_session, run_id=run.id)
    assert job is not None
    source = await claim_memory_job(
        db_session,
        job_id=job.id,
        worker_id="memory-test",
        lease_s=30,
        max_attempts=3,
    )
    assert source is not None
    return source


def test_memory_parsers_fail_closed_and_reject_unknown_targets() -> None:
    candidates = parse_memory_candidates(
        "前缀 "
        + json.dumps(
            {
                "facts": [
                    {
                        "category": "preference",
                        "fact": "  偏好   简洁回答  ",
                        "confidence": 0.9,
                    }
                ]
            },
            ensure_ascii=False,
        )
    )
    assert candidates[0].fact == "偏好 简洁回答"

    allowed = uuid7()
    decision = parse_memory_decision(
        json.dumps(
            {
                "operation": "UPDATE",
                "target_memory_id": str(allowed),
                "reason": "偏好发生变化",
            }
        ),
        allowed_ids={allowed},
    )
    assert decision.target_memory_id == allowed

    with pytest.raises(MemoryExtractionError, match="给定的现有记忆"):
        parse_memory_decision(
            json.dumps(
                {
                    "operation": "DELETE",
                    "target_memory_id": str(uuid7()),
                    "reason": "明确否认",
                }
            ),
            allowed_ids={allowed},
        )


async def test_processing_same_fact_adds_once_then_noops(db_session: AsyncSession) -> None:
    provider = DeterministicProvider()
    gateway = ModelGateway(
        provider,
        embedding_dimensions=1024,
        embedding_revision="memory-test",
    )
    candidate_payload = json.dumps(
        {
            "facts": [
                {
                    "category": "preference",
                    "fact": "偏好简洁回答",
                    "confidence": 0.95,
                }
            ]
        },
        ensure_ascii=False,
    )

    first_source = await _owner_job(db_session, "我偏好简洁回答")
    provider.queue_completions(candidate_payload)
    first_operations = await process_memory_job_source(db_session, gateway, source=first_source)
    assert first_operations[0]["operation"] == "ADD"
    active = await list_memories(db_session)
    assert len(active) == 1

    second_source = await _owner_job(db_session, "还是请保持简洁")
    provider.queue_completions(
        candidate_payload,
        json.dumps(
            {
                "operation": "NOOP",
                "target_memory_id": str(active[0].id),
                "reason": "同一偏好已经存在",
            },
            ensure_ascii=False,
        ),
    )
    second_operations = await process_memory_job_source(db_session, gateway, source=second_source)

    assert second_operations[0]["operation"] == "NOOP"
    refreshed = await list_memories(db_session)
    assert len(refreshed) == 1
    assert refreshed[0].access_count == 1


async def test_disabled_extraction_worker_does_not_claim_queued_jobs() -> None:
    settings = get_settings().model_copy(update={"memory_extraction_enabled": False})
    await memory_extraction_job({"settings": settings}, str(uuid7()))
