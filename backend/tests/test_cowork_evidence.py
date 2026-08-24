from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.cowork.evidence import (
    register_evidence,
    requires_source_grounding,
    validate_final_citations,
)


def _knowledge(quote: str, *, block_id: str | None = None) -> dict[str, Any]:
    return {
        "kind": "knowledge",
        "citation_id": "S1",
        "block_id": block_id or str(uuid4()),
        "version_id": str(uuid4()),
        "document_id": str(uuid4()),
        "title": "论文",
        "source_uri": "paper.pdf",
        "quote": quote,
        "char_start": 10,
        "char_end": 10 + len(quote),
        "heading_path": ["方法"],
        "locations": [],
        "verified": True,
    }


def _reading(locator: int, quote: str, *, verified: bool = False) -> dict[str, Any]:
    return {
        "kind": "reading",
        "citation_id": f"p.{locator}",
        "material_id": "content-hash",
        "title": "论文",
        "source_uri": "/workspace/paper.pdf",
        "quote": quote,
        "locator": locator,
        "verified": verified,
        "locations": [],
    }


def test_search_citation_ids_are_global_and_duplicate_evidence_reuses_id() -> None:
    first_candidate = _knowledge("第一段")
    ledger, first = register_evidence([], [first_candidate], namespace="S", tool_call_id="call-1")
    ledger, second = register_evidence(
        ledger,
        [first_candidate, _knowledge("第二段")],
        namespace="S",
        tool_call_id="call-2",
    )

    assert [item["citation_id"] for item in first] == ["S1"]
    assert [item["citation_id"] for item in second] == ["S1", "S2"]
    assert len(ledger) == 2


def test_unknown_knowledge_reference_is_rejected() -> None:
    ledger, _ = register_evidence(
        [], [_knowledge("有据可查")], namespace="S", tool_call_id="call-1"
    )

    result = validate_final_citations(
        "结论来自原文 [S9]。",
        ledger,
        require_knowledge=True,
        require_reading=False,
    )

    assert not result.ok
    assert "[S9]" in result.errors[0]
    assert result.citations == ()


def test_reading_reference_must_point_to_an_actually_read_locator() -> None:
    ledger, _ = register_evidence(
        [],
        [_reading(12, "整页原文")],
        namespace=None,
        tool_call_id="read-1",
    )

    accepted = validate_final_citations(
        "作者在这里给出主要结论 [p.12]。",
        ledger,
        require_knowledge=False,
        require_reading=True,
    )
    rejected = validate_final_citations(
        "作者还在另一页讨论了限制 [p.13]。",
        ledger,
        require_knowledge=False,
        require_reading=True,
    )

    assert accepted.ok
    assert accepted.citations[0]["citation_id"] == "p.12"
    assert not rejected.ok
    assert "[p.13]" in rejected.errors[0]


def test_verified_quote_is_preferred_for_the_same_locator() -> None:
    ledger, _ = register_evidence(
        [], [_reading(3, "一整页很长的原文")], namespace=None, tool_call_id="read"
    )
    ledger, _ = register_evidence(
        ledger,
        [_reading(3, "逐字核验的原句", verified=True)],
        namespace=None,
        tool_call_id="goto",
    )

    result = validate_final_citations(
        "结论 [p.3]。", ledger, require_knowledge=False, require_reading=True
    )

    assert result.ok
    assert result.citations[0]["quote"] == "逐字核验的原句"
    assert result.citations[0]["verified"] is True
    assert len(result.citations[0]["quote_sha256"]) == 64


def test_citation_examples_inside_code_are_not_treated_as_claims() -> None:
    result = validate_final_citations(
        "示例：`[S99]`\n```text\n[p.999]\n```",
        [],
        require_knowledge=False,
        require_reading=False,
    )

    assert result.ok


def test_explicit_no_evidence_answer_can_fail_closed_without_a_fake_citation() -> None:
    result = validate_final_citations(
        "没有找到足够证据，因此无法基于这份资料确认。",
        [],
        require_knowledge=True,
        require_reading=False,
    )

    assert result.ok


def test_only_pure_social_turns_skip_source_grounding() -> None:
    assert not requires_source_grounding("hello")
    assert not requires_source_grounding("你好！")
    assert not requires_source_grounding("谢谢")
    assert requires_source_grounding("你好，请总结这篇论文")
    assert requires_source_grounding("继续解释第三页")
