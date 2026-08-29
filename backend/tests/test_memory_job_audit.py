from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from uuid6 import uuid7

from app.core.config import get_settings
from app.cowork import memory_policy
from app.cowork.memory import (
    claim_memory_job,
    complete_memory_job,
    get_memory_job,
    retry_or_fail_memory_job,
    schedule_memory_extraction,
)
from app.cowork.memory_extraction import process_memory_job_source
from app.cowork.memory_job_audit import build_memory_job_result
from app.cowork.memory_policy import MEMORY_SAVE_DISABLED_BY_OWNER
from app.cowork_contracts import (
    MEMORY_JOB_RESULT_MAX_BYTES,
    MEMORY_JOB_RESULT_MAX_OPERATIONS,
    MEMORY_JOB_RESULT_SCHEMA,
    MemoryExtractionJob,
    normalize_memory_job_result,
)
from app.cowork_store.routing import cowork_store
from app.runstore.runs import ensure_conversation
from app.worker import memory_run
from app.worker.memory_run import memory_extraction_job
from tests.fakes import DeterministicProvider
from workpilot_ai.gateway import ModelGateway


def _valid_result() -> dict[str, Any]:
    return {
        "schema_version": MEMORY_JOB_RESULT_SCHEMA,
        "status": "completed",
        "skipped_reason": None,
        "operations": [],
        "truncated_operations": 0,
    }


async def _claimed_job(content: str) -> MemoryExtractionJob:
    conversation_id = await ensure_conversation(AsyncMock(), title="Memory result audit")
    job = await schedule_memory_extraction(
        run_id=uuid7(),
        conversation_id=conversation_id,
        source_message_id=uuid7(),
        content=content,
        source_created_at=datetime.now(UTC),
    )
    assert job is not None
    source = await claim_memory_job(
        job_id=job.id,
        worker_id="memory-result-test",
        lease_s=30,
        max_attempts=3,
    )
    assert source is not None
    return source


def test_job_audit_projection_strips_candidate_fact_content_and_confidence() -> None:
    secret = "sk-super-secret-value-that-must-not-persist"
    result = build_memory_job_result(
        [
            {
                "operation": "SKIP",
                "applied": False,
                "current_changed": False,
                "skipped": True,
                "skipped_reason": "credential_or_secret_never_auto_saved",
                "target_memory_id": None,
                "memory_id": None,
                "category": "fact",
                "scope": "global",
                "fact": secret,
                "content": secret,
                "confidence": 0.99,
                "reason": "credential_or_secret_never_auto_saved",
            }
        ]
    )
    encoded = json.dumps(result, ensure_ascii=False)

    assert secret not in encoded
    assert '"fact":' not in encoded
    assert '"content":' not in encoded
    assert '"confidence":' not in encoded
    assert result["operations"][0]["skipped_reason"] == ("credential_or_secret_never_auto_saved")


def test_job_result_contract_rejects_extra_sensitive_fields_and_is_bounded() -> None:
    with pytest.raises(ValueError, match="必须是对象"):
        normalize_memory_job_result([])  # type: ignore[arg-type]

    unsafe = _valid_result()
    unsafe["fact"] = "raw secret"
    with pytest.raises(ValueError, match="未允许字段"):
        normalize_memory_job_result(unsafe)

    many = [
        {
            "operation": "SKIP",
            "skipped": True,
            "skipped_reason": "sensitive_health_or_medical_requires_explicit_memory_consent",
            "category": "fact",
            "scope": "conversation",
            "reason": "药" * 10_000,
        }
        for _ in range(MEMORY_JOB_RESULT_MAX_OPERATIONS + 8)
    ]
    bounded = build_memory_job_result(many)
    encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode()

    assert len(bounded["operations"]) == MEMORY_JOB_RESULT_MAX_OPERATIONS
    assert bounded["truncated_operations"] == 8
    assert len(encoded) <= MEMORY_JOB_RESULT_MAX_BYTES


@pytest.mark.parametrize(
    ("source_content", "candidate", "expected_reason"),
    [
        (
            "请记住这个值：sk-abcdefghijklmnopqrstuvwxyz012345",
            "sk-abcdefghijklmnopqrstuvwxyz012345",
            "credential_or_secret_never_auto_saved",
        ),
        (
            "我每天吃二甲双胍",
            "用户每天吃二甲双胍",
            "sensitive_health_or_medical_requires_explicit_memory_consent",
        ),
    ],
)
async def test_sensitive_skip_reason_survives_completion_without_raw_fact(
    source_content: str,
    candidate: str,
    expected_reason: str,
) -> None:
    source = await _claimed_job(source_content)
    provider = DeterministicProvider(
        completion_texts=[
            json.dumps(
                {
                    "facts": [
                        {
                            "category": "fact",
                            "scope": "global",
                            "fact": candidate,
                            "confidence": 0.99,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        ]
    )
    operations = await process_memory_job_source(
        ModelGateway(provider, embedding_dimensions=1024, embedding_revision="audit-test"),
        source=source,
    )

    assert await complete_memory_job(
        job_id=source.id,
        worker_id="memory-result-test",
        result=build_memory_job_result(operations),
    )
    completed = await get_memory_job(job_id=source.id)

    assert completed is not None and completed.status == "done"
    assert completed.content == ""
    assert completed.result is not None
    assert completed.result["operations"][0]["skipped_reason"] == expected_reason
    assert candidate not in json.dumps(completed.result, ensure_ascii=False)


async def test_worker_rechecks_save_policy_after_claim_and_persists_skip_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = await ensure_conversation(AsyncMock(), title="Policy changed after enqueue")
    job = await schedule_memory_extraction(
        run_id=uuid7(),
        conversation_id=conversation_id,
        source_message_id=uuid7(),
        content="我偏好简洁回答",
        source_created_at=datetime.now(UTC),
    )
    assert job is not None
    await memory_policy.set_owner_memory_policy(
        save_enabled=False,
        recall_enabled=True,
        standing_rules="",
        expected_revision=0,
    )
    build_gateway = AsyncMock()
    monkeypatch.setattr(memory_run, "build_conversation_gateway", build_gateway)

    await memory_extraction_job({"settings": get_settings()}, str(job.id))

    completed = await get_memory_job(job_id=job.id)
    assert completed is not None and completed.status == "done"
    assert completed.result == {
        "schema_version": MEMORY_JOB_RESULT_SCHEMA,
        "status": "skipped",
        "skipped_reason": MEMORY_SAVE_DISABLED_BY_OWNER,
        "operations": [],
        "truncated_operations": 0,
    }
    build_gateway.assert_not_awaited()


async def test_store_rejects_unsafe_result_and_retry_clears_stale_result(store_sql) -> None:
    source = await _claimed_job("我偏好简洁回答")
    unsafe = _valid_result()
    unsafe["content"] = "raw user content"
    with pytest.raises(ValueError, match="未允许字段"):
        await complete_memory_job(
            job_id=source.id,
            worker_id="memory-result-test",
            result=unsafe,
        )
    running = await get_memory_job(job_id=source.id)
    assert running is not None and running.status == "running" and running.result is None

    store_sql(
        "UPDATE memory_extraction_jobs SET result_json = ? WHERE id = ?",
        (json.dumps(_valid_result()), str(source.id)),
    )
    state = await retry_or_fail_memory_job(
        job_id=source.id,
        worker_id="memory-result-test",
        error="temporary provider failure",
        max_attempts=3,
        retry_delay_s=0,
    )
    queued = await get_memory_job(job_id=source.id)

    assert state == "queued"
    assert queued is not None and queued.result is None


async def test_retry_error_is_bounded_and_terminal_failure_clears_source_content() -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    bare_secret = "0123456789abcdef0123456789abcdef0123456789abcdef"
    source = await _claimed_job(f"用户原始敏感消息 {secret}")
    state = await retry_or_fail_memory_job(
        job_id=source.id,
        worker_id="memory-result-test",
        error=f"Authorization: Bearer {secret}\ntrace={bare_secret}\n" + "诊断" * 2_000,
        max_attempts=2,
        retry_delay_s=0,
    )
    queued = await get_memory_job(job_id=source.id)
    assert state == "queued"
    assert queued is not None and queued.content.endswith(secret)
    assert queued.error is not None and secret not in queued.error
    assert bare_secret not in queued.error
    assert len(queued.error) <= 500

    claimed = await claim_memory_job(
        job_id=source.id,
        worker_id="memory-result-test",
        lease_s=30,
        max_attempts=2,
    )
    assert claimed is not None
    state = await retry_or_fail_memory_job(
        job_id=source.id,
        worker_id="memory-result-test",
        error=f"provider echoed {secret}",
        max_attempts=2,
        retry_delay_s=0,
    )
    failed = await get_memory_job(job_id=source.id)
    assert state == "failed"
    assert failed is not None
    assert failed.content == ""
    assert failed.error == "memory_extraction_failed"
    assert secret not in json.dumps(failed.result, ensure_ascii=False)


async def test_deleting_conversation_purges_queued_memory_source() -> None:
    conversation_id = await ensure_conversation(AsyncMock(), title="Delete memory source")
    job = await schedule_memory_extraction(
        run_id=uuid7(),
        conversation_id=conversation_id,
        source_message_id=uuid7(),
        content="删除会话后不能残留的完整消息",
        source_created_at=datetime.now(UTC),
    )
    assert job is not None

    assert await cowork_store().delete_conversation(conversation_id=conversation_id)
    assert await get_memory_job(job_id=job.id) is None
