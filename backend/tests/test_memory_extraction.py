import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from uuid6 import uuid7

from app.core.config import get_settings
from app.cowork.memory import (
    claim_memory_job,
    list_curated_memories,
    schedule_memory_extraction,
)
from app.cowork.memory_extraction import (
    MemoryExtractionError,
    parse_memory_candidates,
    parse_memory_decision,
    process_memory_job_source,
)
from app.cowork_contracts import MemoryExtractionJob
from app.worker import memory_run
from app.worker.memory_run import memory_extraction_job
from tests.fakes import DeterministicProvider
from workpilot_ai.gateway import ModelGateway

pytestmark = pytest.mark.usefixtures("local_cowork_store")


async def _claimed_job(content: str) -> MemoryExtractionJob:
    job = await schedule_memory_extraction(
        run_id=uuid7(),
        conversation_id=uuid7(),
        source_message_id=uuid7(),
        content=content,
        source_created_at=datetime.now(UTC),
    )
    assert job is not None
    source = await claim_memory_job(
        job_id=job.id, worker_id="memory-test", lease_s=30, max_attempts=3
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


async def test_processing_same_fact_adds_once_then_noops() -> None:
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

    first_source = await _claimed_job("我偏好简洁回答")
    provider.queue_completions(candidate_payload)
    first_operations = await process_memory_job_source(gateway, source=first_source)
    assert first_operations[0]["operation"] == "ADD"
    active = await list_curated_memories(active=True)
    assert len(active) == 1

    second_source = await _claimed_job("还是请保持简洁")
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
    second_operations = await process_memory_job_source(gateway, source=second_source)

    assert second_operations[0]["operation"] == "NOOP"
    refreshed = await list_curated_memories(active=True)
    assert len(refreshed) == 1
    assert refreshed[0].access_count == 1


async def test_disabled_extraction_worker_does_not_claim_queued_jobs() -> None:
    settings = get_settings().model_copy(update={"memory_extraction_enabled": False})
    await memory_extraction_job({"settings": settings}, str(uuid7()))


async def test_extraction_worker_reuses_the_source_conversation_provider(monkeypatch) -> None:
    conversation_id = uuid7()
    job = await schedule_memory_extraction(
        run_id=uuid7(),
        conversation_id=conversation_id,
        source_message_id=uuid7(),
        content="请记住我偏好简洁回答",
        source_created_at=datetime.now(UTC),
    )
    assert job is not None

    gateway = AsyncMock()
    build_gateway = AsyncMock(return_value=gateway)
    process = AsyncMock()
    monkeypatch.setattr(memory_run, "build_conversation_gateway", build_gateway)
    monkeypatch.setattr(memory_run, "process_memory_job_source", process)

    @asynccontextmanager
    async def fake_session_factory():
        yield object()

    await memory_extraction_job(
        {
            "settings": get_settings(),
            "session_factory": fake_session_factory,
        },
        str(job.id),
    )

    assert build_gateway.await_args.kwargs["conversation_id"] == conversation_id
    process.assert_awaited_once()
    gateway.aclose.assert_awaited_once()
