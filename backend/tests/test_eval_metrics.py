from uuid import UUID

import pytest
from eval.mapping import GoldSpan, RetrievedChunk, hits, overlap_ratio
from eval.metrics.refusal import analyze_refusal
from eval.metrics.retrieval import evaluate_retrieval

VERSION_A = UUID("00000000-0000-0000-0000-000000000001")
VERSION_B = UUID("00000000-0000-0000-0000-000000000002")


def _chunk(
    number: int,
    start: int,
    end: int,
    *,
    version: UUID = VERSION_A,
    tokens: int = 100,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=UUID(f"00000000-0000-0000-0000-{number:012d}"),
        version_id=version,
        char_start=start,
        char_end=end,
        content_tokens=tokens,
        score=1 - number / 100,
    )


def test_mapping_uses_gold_span_coverage_and_version_identity() -> None:
    span = GoldSpan(version_id=VERSION_A, char_start=100, char_end=200)

    assert overlap_ratio(_chunk(1, 50, 150), span) == 0.5
    assert hits(_chunk(1, 50, 150), span, theta=0.5) is True
    assert hits(_chunk(2, 100, 200, version=VERSION_B), span, theta=0.5) is False


def test_retrieval_metrics_cover_ranking_budget_and_duplicate_evidence() -> None:
    spans = [
        GoldSpan(version_id=VERSION_A, char_start=100, char_end=200),
        GoldSpan(version_id=VERSION_A, char_start=300, char_end=400),
    ]
    irrelevant = _chunk(1, 500, 600)
    first = _chunk(2, 90, 210, tokens=120)
    duplicate = _chunk(3, 100, 180, tokens=120)
    second = _chunk(4, 290, 410, tokens=120)
    candidates = [first, duplicate, second, irrelevant]

    metrics = evaluate_retrieval(
        spans,
        [irrelevant, first, duplicate, second],
        candidates,
        top_k=4,
        token_budget=240,
        theta=0.5,
        alpha=0.5,
    )

    assert metrics.span_recall_at_k == 1.0
    assert metrics.budget_span_recall == 0.5
    assert metrics.mrr == 0.5
    assert metrics.context_precision == 0.75
    assert 0 < metrics.ndcg_at_k < 1
    assert 0 < metrics.alpha_ndcg_at_k < 1


def test_refusal_analysis_finds_separating_threshold() -> None:
    analysis = analyze_refusal(
        [(0.82, True), (0.76, True), (0.31, False), (0.18, False)],
        configured_threshold=0.35,
    )

    assert analysis.auroc == 1.0
    assert analysis.best is not None
    assert analysis.best.macro_f1 == 1.0
    assert 0.31 < analysis.best.threshold < 0.76
    assert analysis.configured is not None
    assert analysis.configured.false_answerable == 0


def test_answerable_metrics_require_gold_spans() -> None:
    with pytest.raises(ValueError, match="gold span"):
        evaluate_retrieval([], [], [], top_k=10, token_budget=1000)
