from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app.cowork.semantic_approvals import (
    build_semantic_approval_evidence,
    build_trusted_approval_evidence,
    canonical_external_action,
    review_semantic_action,
    verify_semantic_approval_evidence,
)
from workpilot_ai.types import CacheRetention, CompletionResult, Message, Usage


class _ReviewGateway:
    def __init__(self, result: str | Exception) -> None:
        self.result = result
        self.calls: list[list[Message]] = []

    async def complete(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
        cache_retention: CacheRetention = "default",
        session_id: str | None = None,
    ) -> CompletionResult:
        assert task_type == "cowork_semantic_approval"
        assert max_tokens == 32
        assert temperature == 0.0
        assert cache_retention == "none"
        assert session_id is not None and session_id.startswith("semantic-approval:")
        self.calls.append(messages)
        if isinstance(self.result, Exception):
            raise self.result
        return CompletionResult(
            text=self.result,
            model="review-model",
            provider="review-provider",
            usage=Usage(input_tokens=4, output_tokens=2),
        )


@pytest.mark.asyncio
async def test_semantic_reviewer_envelope_excludes_action_body_and_history() -> None:
    gateway = _ReviewGateway('{"decision":"allow"}')
    action = canonical_external_action(
        tool="connector_call",
        risk="external",
        effect="external",
        target='{"account_id":"acct-1","action":"send"}',
        arguments_sha256="a" * 64,
    )

    result = await review_semantic_action(
        gateway,
        session_facts={"schema_version": "session_facts.v1", "workspace_roots": []},
        user_text="给 acct-1 发送这条消息",
        action=action,
    )

    assert result.decision == "allow"
    envelope = json.loads(gateway.calls[0][1].content)
    assert set(envelope) == {
        "schema",
        "frozen_session_facts",
        "user_authored_text",
        "user_text_truncated",
        "canonical_action",
    }
    encoded = gateway.calls[0][1].content
    assert "attachment" not in encoded
    assert "assistant" not in encoded
    assert "tool_result" not in encoded
    assert "secret message body" not in encoded
    assert set(envelope["canonical_action"]) == {
        "schema",
        "kind",
        "tool",
        "risk",
        "effect",
        "arguments_sha256",
        "target",
        "arguments_opaque",
        "target_truncated",
    }
    assert envelope["canonical_action"]["arguments_opaque"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result, disposition",
    [
        ("not-json", "invalid_response"),
        ('{"decision":"allow","reason":"extra"}', "invalid_response"),
        (RuntimeError("provider leaked secret"), "provider_error"),
    ],
)
async def test_semantic_reviewer_is_strict_and_fail_closed(
    result: str | Exception, disposition: str
) -> None:
    review = await review_semantic_action(
        _ReviewGateway(result),
        session_facts={},
        user_text="do something",
        action={"tool": "x", "arguments_sha256": "b" * 64},
    )

    assert review.decision == "unsure"
    assert review.disposition == disposition


def test_semantic_approval_evidence_is_bound_to_exact_run_call_and_action() -> None:
    key = "1" * 64
    run_id = uuid4()
    evidence = build_semantic_approval_evidence(
        signing_key=key,
        run_id=run_id,
        tool_call_id="call-1",
        tool="connector_call",
        arguments_sha256="c" * 64,
        review_receipt_id="d" * 64,
    )

    assert verify_semantic_approval_evidence(
        evidence,
        signing_key=key,
        run_id=run_id,
        tool_call_id="call-1",
        tool="connector_call",
        arguments_sha256="c" * 64,
    )
    tampered = {**evidence, "arguments_sha256": "e" * 64}
    assert not verify_semantic_approval_evidence(
        tampered,
        signing_key=key,
        run_id=run_id,
        tool_call_id="call-1",
        tool="connector_call",
        arguments_sha256="e" * 64,
    )


def test_approval_evidence_rejects_unknown_sources_and_binds_trusted_sources() -> None:
    key = "2" * 64
    run_id = uuid4()
    arguments_hash = "a" * 64
    trusted = build_trusted_approval_evidence(
        signing_key=key,
        source="user",
        run_id=run_id,
        tool_call_id="call-user",
        tool="run_shell",
        arguments_sha256=arguments_hash,
        details={"inbox_id": str(uuid4()), "standing_rule_id": None},
    )

    assert verify_semantic_approval_evidence(
        trusted,
        signing_key=key,
        run_id=run_id,
        tool_call_id="call-user",
        tool="run_shell",
        arguments_sha256=arguments_hash,
    )
    assert not verify_semantic_approval_evidence(
        {**trusted, "source": "future_typo"},
        signing_key=key,
        run_id=run_id,
        tool_call_id="call-user",
        tool="run_shell",
        arguments_sha256=arguments_hash,
    )
    assert not verify_semantic_approval_evidence(
        {**trusted, "tool_call_id": "other-call"},
        signing_key=key,
        run_id=run_id,
        tool_call_id="other-call",
        tool="run_shell",
        arguments_sha256=arguments_hash,
    )
