from uuid import UUID

import pytest
from eval.dense_baseline import ItemResult, _aggregate
from eval.mapping import GoldSpan, RetrievedChunk, hits, overlap_ratio
from eval.metrics.diagnostics import diagnose_spans, summarize_scores
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


def test_span_diagnostics_separate_ranking_index_and_document_failures() -> None:
    spans = [
        GoldSpan(version_id=VERSION_A, char_start=100, char_end=200),
        GoldSpan(version_id=VERSION_A, char_start=300, char_end=400),
        GoldSpan(version_id=VERSION_A, char_start=500, char_end=600),
        GoldSpan(version_id=VERSION_B, char_start=100, char_end=200),
        GoldSpan(version_id=VERSION_B, char_start=300, char_end=400),
    ]
    retrieved = [
        _chunk(1, 90, 210, tokens=100),
        _chunk(2, 290, 410, tokens=100),
        _chunk(3, 700, 800),
    ]
    candidates = [
        *retrieved,
        _chunk(4, 490, 610),
        _chunk(5, 90, 210, version=VERSION_B),
    ]

    diagnostics = diagnose_spans(
        spans,
        retrieved,
        candidates,
        top_k=2,
        token_budget=100,
        theta=0.5,
    )

    assert [item.status for item in diagnostics] == [
        "hit",
        "outside_token_budget",
        "relevant_chunk_not_ranked",
        "document_not_retrieved",
        "no_relevant_indexed_chunk",
    ]
    assert diagnostics[1].first_hit_rank == 2
    assert diagnostics[2].mapped_chunk_count == 1


def test_span_diagnostics_identify_hits_below_formal_top_k() -> None:
    span = GoldSpan(version_id=VERSION_A, char_start=300, char_end=400)
    diagnostics = diagnose_spans(
        [span],
        [_chunk(1, 500, 600), _chunk(2, 290, 410)],
        [_chunk(2, 290, 410)],
        top_k=1,
        token_budget=1000,
        theta=0.5,
    )

    assert diagnostics[0].status == "outside_top_k"
    assert diagnostics[0].first_hit_rank == 2


def test_score_summary_includes_quantiles_and_histogram() -> None:
    summary = summarize_scores([0.31, 0.34, 0.51, 0.56])

    assert summary["count"] == 4
    assert summary["median"] == pytest.approx(0.425)
    assert summary["histogram"] == {
        "[0.30,0.35)": 2,
        "[0.50,0.55)": 1,
        "[0.55,0.60)": 1,
    }
    with pytest.raises(ValueError, match="histogram_step"):
        summarize_scores([0.5], histogram_step=0)


def test_aggregate_reports_category_metrics_and_refusal_distributions() -> None:
    def result(number: int, category: str, answerable: bool, score: float) -> ItemResult:
        retrieval = (
            {
                "span_recall_at_k": 1.0,
                "budget_span_recall": 1.0,
                "ndcg_at_k": 1.0,
                "alpha_ndcg_at_k": 1.0,
                "mrr": 1.0,
                "context_precision": 0.5,
            }
            if answerable
            else None
        )
        return ItemResult(
            item_id=UUID(f"00000000-0000-0000-0001-{number:012d}"),
            category=category,
            question=f"q{number}",
            answerable=answerable,
            top_score=score,
            latency_ms=number,
            retrieval=retrieval,
            retrieved=[],
            span_diagnostics=([{"status": "hit"}] if answerable else []),
        )

    results = [
        result(1, "single_hop", True, 0.8),
        result(2, "single_hop", True, 0.7),
        result(3, "unanswerable", False, 0.3),
    ]
    refusal = analyze_refusal(
        [(item.top_score, item.answerable) for item in results],
        configured_threshold=0.35,
    )

    aggregate = _aggregate(results, refusal, configured_threshold=0.35)

    categories = aggregate["by_category"]
    assert isinstance(categories, dict)
    assert categories["single_hop"]["item_count"] == 2
    assert categories["unanswerable"]["configured_refusal"]["refused_count"] == 1
    refusal_metrics = aggregate["refusal"]
    assert isinstance(refusal_metrics, dict)
    assert refusal_metrics["score_distributions"]["answerable"]["count"] == 2
