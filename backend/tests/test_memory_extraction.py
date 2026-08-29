import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from uuid6 import uuid7

from app.core.config import get_settings
from app.cowork import memory_policy
from app.cowork.memory import (
    claim_memory_job,
    list_curated_memories,
    remember,
    schedule_memory_extraction,
)
from app.cowork.memory_extraction import (
    MemoryExtractionError,
    parse_memory_candidates,
    parse_memory_decision,
    process_memory_job_source,
)
from app.cowork.memory_policy import OwnerMemoryPolicy
from app.cowork_contracts import MemoryExtractionJob
from app.runstore.runs import ensure_conversation
from app.worker import memory_run
from app.worker.memory_run import memory_extraction_job
from tests.fakes import DeterministicProvider
from workpilot_ai.gateway import ModelGateway

pytestmark = pytest.mark.usefixtures("local_cowork_store")


async def _conversation(title: str = "Memory extraction") -> UUID:
    return await ensure_conversation(AsyncMock(), title=title)


async def _claimed_job(content: str, *, conversation_id: UUID | None = None) -> MemoryExtractionJob:
    job = await schedule_memory_extraction(
        run_id=uuid7(),
        conversation_id=conversation_id or await _conversation(),
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


def _candidate_payload(fact: str, *, category: str = "fact") -> str:
    return json.dumps(
        {
            "facts": [
                {
                    "category": category,
                    "scope": "global",
                    "fact": fact,
                    "confidence": 0.99,
                }
            ]
        },
        ensure_ascii=False,
    )


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
    assert candidates[0].scope == "conversation"

    scoped = parse_memory_candidates(
        json.dumps(
            {
                "facts": [
                    {
                        "category": "fact",
                        "scope": "workspace",
                        "fact": "本项目使用 Ruff",
                        "confidence": 0.9,
                    }
                ]
            },
            ensure_ascii=False,
        )
    )
    assert scoped[0].scope == "workspace"
    with pytest.raises(MemoryExtractionError, match="scope"):
        parse_memory_candidates(
            json.dumps(
                {
                    "facts": [
                        {
                            "category": "fact",
                            "scope": "tenant",
                            "fact": "越界 scope",
                            "confidence": 0.9,
                        }
                    ]
                }
            )
        )

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


@pytest.mark.parametrize(
    ("source_text", "candidate_fact", "reason"),
    [
        (
            "我被诊断为糖尿病",
            "用户被诊断为糖尿病",
            "sensitive_health_or_medical_requires_explicit_memory_consent",
        ),
        (
            "我每天吃二甲双胍",
            "用户每天吃二甲双胍",
            "sensitive_health_or_medical_requires_explicit_memory_consent",
        ),
        (
            "I take metformin daily",
            "The user takes metformin daily",
            "sensitive_health_or_medical_requires_explicit_memory_consent",
        ),
        (
            "我的年薪是 80 万元",
            "用户年薪为 80 万元",
            "sensitive_financial_requires_explicit_memory_consent",
        ),
        (
            "我的妻子叫小林",
            "用户的妻子叫小林",
            "sensitive_relationship_or_family_requires_explicit_memory_consent",
        ),
        (
            "我是佛教徒",
            "用户信仰佛教",
            "sensitive_religion_or_politics_requires_explicit_memory_consent",
        ),
        (
            "我的政治立场偏保守派",
            "用户的政治立场偏保守派",
            "sensitive_religion_or_politics_requires_explicit_memory_consent",
        ),
    ],
)
async def test_deterministic_gate_skips_sensitive_model_candidates_without_consent(
    source_text: str,
    candidate_fact: str,
    reason: str,
) -> None:
    provider = DeterministicProvider()
    provider.queue_completions(_candidate_payload(candidate_fact))
    gateway = ModelGateway(provider, embedding_dimensions=1024, embedding_revision="privacy-test")

    operations = await process_memory_job_source(
        gateway,
        source=await _claimed_job(source_text),
    )

    assert operations == [
        {
            "operation": "SKIP",
            "applied": False,
            "current_changed": False,
            "skipped": True,
            "skipped_reason": reason,
            "target_memory_id": None,
            "memory_id": None,
            "category": "fact",
            "scope": "global",
            "fact": candidate_fact,
            "confidence": 0.99,
            "reason": reason,
        }
    ]
    assert await list_curated_memories(active=True) == []
    # 这是模型确实返回候选后的服务端拦截，不是只依赖抽取 prompt 不返回候选。
    assert provider.completion_texts == []
    assert "服务端还会做独立的确定性门禁" in provider.last_messages[0].content


async def test_credentials_are_never_auto_saved_even_with_explicit_consent() -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    fact = f"用户的 API 密钥是 {secret}"
    provider = DeterministicProvider(completion_texts=[_candidate_payload(fact)])
    gateway = ModelGateway(provider, embedding_dimensions=1024, embedding_revision="privacy-test")

    operations = await process_memory_job_source(
        gateway,
        source=await _claimed_job(f"请记住我的 API 密钥是 {secret}"),
    )

    assert operations[0]["operation"] == "SKIP"
    assert operations[0]["skipped_reason"] == "credential_or_secret_never_auto_saved"
    assert await list_curated_memories(active=True) == []


@pytest.mark.parametrize(
    "secret",
    [
        "9f86d081884c7d659a2feaa0c55ad015",
        "QWxhZGRpbjpvcGVuIHNlc2FtZTEyMzQ1Njc4OTA=",
        "AbCdEfGh12_34567.IjKlMnOp34_56789.QrStUvWx56_78901",
    ],
)
async def test_bare_high_entropy_credentials_are_never_auto_saved(secret: str) -> None:
    # 模型故意去掉“密钥/token”等标签，只输出高熵值，服务端仍需 fail closed。
    provider = DeterministicProvider(completion_texts=[_candidate_payload(secret)])
    gateway = ModelGateway(provider, embedding_dimensions=1024, embedding_revision="privacy-test")

    operations = await process_memory_job_source(
        gateway,
        source=await _claimed_job(f"请记住这个值：{secret}"),
    )

    assert operations[0]["operation"] == "SKIP"
    assert operations[0]["skipped_reason"] == "credential_or_secret_never_auto_saved"
    assert await list_curated_memories(active=True) == []


async def test_sensitive_candidate_requires_consent_for_that_fact_not_another_clause() -> None:
    fact = "用户被诊断为糖尿病"
    provider = DeterministicProvider(completion_texts=[_candidate_payload(fact)])
    gateway = ModelGateway(provider, embedding_dimensions=1024, embedding_revision="privacy-test")

    operations = await process_memory_job_source(
        gateway,
        source=await _claimed_job("请记住我喜欢蓝色；我被诊断为糖尿病"),
    )

    assert operations[0]["operation"] == "SKIP"
    assert operations[0]["skipped_reason"].startswith("sensitive_health_or_medical")
    assert await list_curated_memories(active=True) == []


async def test_explicit_consent_allows_noncredential_sensitive_candidate() -> None:
    fact = "用户被诊断为糖尿病"
    provider = DeterministicProvider(completion_texts=[_candidate_payload(fact)])
    gateway = ModelGateway(provider, embedding_dimensions=1024, embedding_revision="privacy-test")

    operations = await process_memory_job_source(
        gateway,
        source=await _claimed_job("记住我被诊断为糖尿病"),
    )

    assert operations[0]["operation"] == "ADD"
    active = await list_curated_memories(active=True)
    assert [item.content for item in active] == [fact]


async def test_negated_memory_request_does_not_count_as_sensitive_consent() -> None:
    fact = "用户被诊断为糖尿病"
    provider = DeterministicProvider(completion_texts=[_candidate_payload(fact)])
    gateway = ModelGateway(provider, embedding_dimensions=1024, embedding_revision="privacy-test")

    operations = await process_memory_job_source(
        gateway,
        source=await _claimed_job("请不要记住我被诊断为糖尿病"),
    )

    assert operations[0]["operation"] == "SKIP"
    assert await list_curated_memories(active=True) == []


async def test_auto_write_rechecks_save_policy_after_model_returns_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SaveOffStore:
        async def get_owner_memory_policy(self) -> OwnerMemoryPolicy:
            return OwnerMemoryPolicy(save_enabled=False)

        async def get_conversation_memory_policy(self, *, conversation_id: UUID) -> None:
            _ = conversation_id
            return None

    fact = "用户偏好先给结论"
    provider = DeterministicProvider(completion_texts=[_candidate_payload(fact)])
    gateway = ModelGateway(provider, embedding_dimensions=1024, embedding_revision="policy-test")
    monkeypatch.setattr(memory_policy, "_policy_store", lambda: SaveOffStore())

    operations = await process_memory_job_source(
        gateway,
        source=await _claimed_job("我偏好先给结论"),
    )

    assert provider.completion_texts == []  # provider 确实已经输出候选
    assert operations[0]["operation"] == "SKIP"
    assert operations[0]["requested_operation"] == "ADD"
    assert operations[0]["skipped_reason"] == "memory_save_disabled_by_owner"
    assert await list_curated_memories(active=True) == []


async def test_save_off_blocks_background_classifier_delete_and_preserves_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SaveOffStore:
        async def get_owner_memory_policy(self) -> OwnerMemoryPolicy:
            return OwnerMemoryPolicy(save_enabled=False)

        async def get_conversation_memory_policy(self, *, conversation_id: UUID) -> None:
            _ = conversation_id
            return None

    conversation_id = await _conversation("Classifier delete policy")
    existing, _ = await remember(
        AsyncMock(),
        conversation_id=conversation_id,
        scope="global",
        content="用户偏好先给结论",
    )
    candidate = "用户不再偏好先给结论"
    provider = DeterministicProvider(
        completion_texts=[
            _candidate_payload(candidate),
            json.dumps(
                {
                    "operation": "DELETE",
                    "target_memory_id": str(existing.id),
                    "reason": "用户明确否认旧偏好",
                },
                ensure_ascii=False,
            ),
        ]
    )
    gateway = ModelGateway(provider, embedding_dimensions=1024, embedding_revision="policy-test")
    monkeypatch.setattr(memory_policy, "_policy_store", lambda: SaveOffStore())

    operations = await process_memory_job_source(
        gateway,
        source=await _claimed_job(
            "我不再偏好先给结论",
            conversation_id=conversation_id,
        ),
    )

    assert operations[0]["operation"] == "SKIP"
    assert operations[0]["requested_operation"] == "DELETE"
    assert operations[0]["skipped_reason"] == "memory_save_disabled_by_owner"
    active = await list_curated_memories(active=True)
    assert [item.id for item in active] == [existing.id]
    assert active[0].content == "用户偏好先给结论"


async def test_save_off_blocks_background_noop_usage_metadata_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SaveOffStore:
        async def get_owner_memory_policy(self) -> OwnerMemoryPolicy:
            return OwnerMemoryPolicy(save_enabled=False)

        async def get_conversation_memory_policy(self, *, conversation_id: UUID) -> None:
            _ = conversation_id
            return None

    conversation_id = await _conversation("Classifier noop policy")
    existing, _ = await remember(
        AsyncMock(),
        conversation_id=conversation_id,
        scope="global",
        content="用户偏好先给结论",
    )
    provider = DeterministicProvider(
        completion_texts=[
            _candidate_payload(existing.content),
            json.dumps(
                {
                    "operation": "NOOP",
                    "target_memory_id": str(existing.id),
                    "reason": "事实已经存在",
                },
                ensure_ascii=False,
            ),
        ]
    )
    gateway = ModelGateway(provider, embedding_dimensions=1024, embedding_revision="policy-test")
    monkeypatch.setattr(memory_policy, "_policy_store", lambda: SaveOffStore())

    operations = await process_memory_job_source(
        gateway,
        source=await _claimed_job("我仍然偏好先给结论", conversation_id=conversation_id),
    )

    assert operations[0]["operation"] == "SKIP"
    assert operations[0]["requested_operation"] == "NOOP"
    active = await list_curated_memories(active=True)
    assert active[0].id == existing.id
    assert active[0].access_count == 0


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

    conversation_id = await _conversation("Same fact")
    first_source = await _claimed_job("我偏好简洁回答", conversation_id=conversation_id)
    provider.queue_completions(candidate_payload)
    first_operations = await process_memory_job_source(gateway, source=first_source)
    assert first_operations[0]["operation"] == "ADD"
    active = await list_curated_memories(active=True)
    assert len(active) == 1

    second_source = await _claimed_job("还是请保持简洁", conversation_id=conversation_id)
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
    assert refreshed[0].scope == "conversation"
    assert refreshed[0].conversation_id == conversation_id


async def test_auto_extraction_uses_workspace_scope_only_with_an_authorized_root() -> None:
    provider = DeterministicProvider()
    gateway = ModelGateway(provider, embedding_dimensions=1024, embedding_revision="scope-test")
    payload = json.dumps(
        {
            "facts": [
                {
                    "category": "fact",
                    "scope": "workspace",
                    "fact": "本项目提交前运行 Ruff",
                    "confidence": 0.94,
                }
            ]
        },
        ensure_ascii=False,
    )

    workspace_conversation = await _conversation("Workspace scope")
    provider.queue_completions(payload)
    await process_memory_job_source(
        gateway,
        source=await _claimed_job("本项目提交前运行 Ruff", conversation_id=workspace_conversation),
        workspace_paths=("/authorized/project",),
    )
    workspace_memory = (await list_curated_memories(active=True))[0]
    assert workspace_memory.scope == "workspace"
    assert workspace_memory.workspace_path == "/authorized/project"

    updated_payload = json.dumps(
        {
            "facts": [
                {
                    "category": "fact",
                    "scope": "workspace",
                    "fact": "本项目提交前运行 Ruff 和单元测试",
                    "confidence": 0.96,
                }
            ]
        },
        ensure_ascii=False,
    )
    provider.queue_completions(
        updated_payload,
        json.dumps(
            {
                "operation": "UPDATE",
                "target_memory_id": str(workspace_memory.id),
                "reason": "项目约定被明确补充",
            },
            ensure_ascii=False,
        ),
    )
    await process_memory_job_source(
        gateway,
        source=await _claimed_job(
            "改为提交前运行 Ruff 和单元测试",
            conversation_id=workspace_conversation,
        ),
        workspace_paths=("/authorized/project",),
    )
    current_workspace = next(
        item
        for item in await list_curated_memories(active=True)
        if item.content.endswith("Ruff 和单元测试")
    )
    assert current_workspace.scope == "workspace"
    assert current_workspace.workspace_path == "/authorized/project"

    # 模型建议 workspace 不是授权；没有当前 root 时保守降级为 conversation。
    conversation_only = await _conversation("Conversation fallback")
    provider.queue_completions(payload)
    await process_memory_job_source(
        gateway,
        source=await _claimed_job("本项目提交前运行 Ruff", conversation_id=conversation_only),
    )
    active = await list_curated_memories(active=True)
    scoped = next(item for item in active if item.conversation_id == conversation_only)
    assert scoped.scope == "conversation"
    assert scoped.workspace_path is None

    # 多个 root 时“workspace”没有唯一绑定目标，不能悄悄取列表第一项。
    ambiguous_conversation = await _conversation("Ambiguous workspace")
    provider.queue_completions(payload)
    await process_memory_job_source(
        gateway,
        source=await _claimed_job("本项目提交前运行 Ruff", conversation_id=ambiguous_conversation),
        workspace_paths=("/authorized/one", "/authorized/two"),
    )
    active = await list_curated_memories(active=True)
    ambiguous = next(item for item in active if item.conversation_id == ambiguous_conversation)
    assert ambiguous.scope == "conversation"
    assert ambiguous.workspace_path is None


async def test_auto_extraction_does_not_classify_against_another_conversation() -> None:
    provider = DeterministicProvider()
    gateway = ModelGateway(provider, embedding_dimensions=1024, embedding_revision="scope-test")
    payload = json.dumps(
        {
            "facts": [
                {
                    "category": "fact",
                    # 旧响应不带 scope，parser 也必须保守成 conversation。
                    "fact": "只在本讨论中使用草案 A",
                    "confidence": 0.9,
                }
            ]
        },
        ensure_ascii=False,
    )

    first = await _conversation("First conversation")
    second = await _conversation("Second conversation")
    provider.queue_completions(payload)
    await process_memory_job_source(
        gateway,
        source=await _claimed_job("使用草案 A", conversation_id=first),
    )
    # 若错误地把另一会话记忆交给 classifier，这里还会消耗第二个 classification completion。
    provider.queue_completions(payload)
    await process_memory_job_source(
        gateway,
        source=await _claimed_job("使用草案 A", conversation_id=second),
    )

    records = [
        item for item in await list_curated_memories(active=True) if item.content.endswith("草案 A")
    ]
    assert {item.conversation_id for item in records} == {first, second}


async def test_disabled_extraction_worker_does_not_claim_queued_jobs() -> None:
    settings = get_settings().model_copy(update={"memory_extraction_enabled": False})
    await memory_extraction_job({"settings": settings}, str(uuid7()))


async def test_extraction_worker_reuses_the_source_conversation_provider(monkeypatch) -> None:
    conversation_id = await _conversation("Worker provider")
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
