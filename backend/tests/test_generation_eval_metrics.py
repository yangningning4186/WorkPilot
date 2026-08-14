from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from eval.citation_review import cited_claims
from eval.m0_report import _human_review
from eval.metrics.generation import (
    CitationSource,
    evaluate_citation_validity,
    evaluate_constraints,
)

from app.retrieval.citations import REFUSAL_TEXT

BLOCK_ID = UUID("00000000-0000-0000-0000-000000000001")
VERSION_ID = UUID("00000000-0000-0000-0000-000000000002")
DOCUMENT_ID = UUID("00000000-0000-0000-0000-000000000003")


@dataclass(frozen=True)
class Citation:
    citation_id: str = "S1"
    block_id: UUID = BLOCK_ID
    version_id: UUID = VERSION_ID
    document_id: UUID = DOCUMENT_ID
    quote: str = "证据原文"
    char_start: int = 2
    char_end: int = 6


def _source() -> CitationSource:
    return CitationSource(
        block_id=BLOCK_ID,
        version_id=VERSION_ID,
        document_id=DOCUMENT_ID,
        block_char_start=0,
        block_char_end=8,
        full_text="前缀证据原文后缀",
    )


def test_citation_validity_accepts_exact_database_quote() -> None:
    result = evaluate_citation_validity(
        answer="结论。[S1]",
        citations=[Citation()],
        sources={BLOCK_ID: _source()},
        refused=False,
    )

    assert result.valid is True
    assert result.citation_count == 1
    assert result.issues == ()


def test_citation_validity_reports_format_reference_object_and_quote_failures() -> None:
    result = evaluate_citation_validity(
        answer="结论。[S0][S2]",
        citations=[Citation(citation_id="S2", quote="错误文本")],
        sources={BLOCK_ID: _source()},
        refused=False,
    )

    assert result.valid is False
    assert result.format_valid is False
    assert result.references_match is True
    assert result.objects_exist is True
    assert result.quotes_match is False
    assert "malformed_label:S0" in result.issues
    assert "quote_mismatch:S2" in result.issues


def test_citation_validity_checks_refusal_contract() -> None:
    accepted = evaluate_citation_validity(
        answer=REFUSAL_TEXT,
        citations=[],
        sources={},
        refused=True,
    )
    rejected = evaluate_citation_validity(
        answer=f"{REFUSAL_TEXT}[S1]",
        citations=[Citation()],
        sources={BLOCK_ID: _source()},
        refused=True,
    )

    assert accepted.valid is True
    assert rejected.valid is False
    assert "refusal_text_mismatch" in rejected.issues
    assert "refusal_has_citations" in rejected.issues


def test_constraint_rules_are_literal_and_case_insensitive() -> None:
    passed = evaluate_constraints(
        "Use FastAPI and docker compose; never use an ORACLE.",
        {"must_include": ["fastapi", "docker compose"], "must_not_include": ["regex.*"]},
    )
    failed = evaluate_constraints(
        "Use FastAPI and Oracle.",
        {"must_include": ["Postgres"], "must_not_include": ["oracle"]},
    )

    assert passed.passed is True
    assert failed.issues == ("missing:Postgres", "forbidden:oracle")


def test_human_citation_accuracy_requires_attributed_complete_review(tmp_path: Path) -> None:
    items = [
        {
            "item_id": "item-1",
            "citations": [{"citation_id": "S1"}, {"citation_id": "S2"}],
        }
    ]
    pending = _human_review(items, [])
    review = tmp_path / "review.csv"
    review.write_text(
        "item_id,citation_id,supported,reason,reviewer,reviewed_at\n"
        "item-1,S1,yes,直接支持,Alice,2026-08-14T12:00:00+08:00\n"
        "item-1,S2,no,证据不支持,Alice,2026-08-14T12:01:00+08:00\n",
        encoding="utf-8",
    )
    complete = _human_review(items, [review])

    assert pending["status"] == "pending_human_review"
    assert pending["rate"] is None
    assert complete["status"] == "complete"
    assert complete["review_coverage"] == 1.0
    assert complete["rate"] == 0.5


def test_citation_review_extracts_only_claims_using_the_label() -> None:
    answer = (
        "Repository https://example.com/repo is required.[S1] Second claim.[S2]\n"
        "第三个结论。[S1][S2]"
    )

    assert cited_claims(answer, "S1") == (
        "Repository https://example.com/repo is required.[S1]\n第三个结论。[S1][S2]"
    )
