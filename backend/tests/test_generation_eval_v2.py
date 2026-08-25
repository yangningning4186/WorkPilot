from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.generation_runner import _evaluate_constraints, _node_overlaps_span
from eval.generation_suite import GenerationGoldSpan, GenerationItem, load_generation_suite
from eval.kb_retrieval_runner import IndexedNode


def test_frozen_generation_suite_is_self_contained_and_approved() -> None:
    suite = load_generation_suite(
        Path(__file__).resolve().parents[2] / "eval/suites/m1-dev-70-v2.json"
    )

    assert suite.name == "m1-dev-70-v2"
    assert suite.reviewer == "行之"
    assert len(suite.items) == 70
    assert len(suite.corpus) == 37
    assert sum(item.answerable for item in suite.items) == 57
    assert sum(not item.answerable for item in suite.items) == 13
    assert min(
        span.migration_match_score
        for item in suite.items
        for group in item.evidence_groups
        for span in group.alternatives
    ) >= 0.60


def test_loader_rejects_unapproved_human_suite(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "eval/suites/m1-dev-70-v2.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["review"] = {"status": "pending_human_review"}
    target = tmp_path / "suite.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="approved"):
        load_generation_suite(target)


def test_citation_alignment_requires_same_content_page_and_overlap() -> None:
    node = IndexedNode(
        node_id="n1",
        chunk_id=__import__("uuid").uuid4(),
        source_id=__import__("uuid").uuid4(),
        document_id=__import__("uuid").uuid4(),
        content_hash="a" * 64,
        filename="paper.pdf",
        page_no=3,
        char_start=100,
        char_end=200,
        text="x" * 100,
        content_tokens=25,
    )
    matching = GenerationGoldSpan(
        content_hash="a" * 64,
        filename="paper.pdf",
        page_no=3,
        char_start=150,
        char_end=220,
        quote="x" * 70,
        migration_match_score=1.0,
    )
    wrong_page = GenerationGoldSpan(
        content_hash="a" * 64,
        filename="paper.pdf",
        page_no=4,
        char_start=150,
        char_end=220,
        quote="x" * 70,
        migration_match_score=1.0,
    )

    assert _node_overlaps_span(node, matching)
    assert not _node_overlaps_span(node, wrong_page)


def test_constraints_are_scored_only_against_answer_text() -> None:
    item = GenerationItem(
        item_id="one",
        dataset_name="dev",
        split="dev",
        category="single_hop",
        difficulty=1,
        question="q",
        gold_answer="a",
        constraints={"must_include": ("37 days",), "must_not_include": ("30 days",)},
        temporal_ctx=None,
        evidence_groups=(),
    )

    assert _evaluate_constraints("Retention is 37 days [S1].", item)["passed"] is True
    failed = _evaluate_constraints("Retention is 30 days [S1].", item)
    assert failed["passed"] is False
    assert set(failed["issues"]) == {"missing:37 days", "forbidden:30 days"}
