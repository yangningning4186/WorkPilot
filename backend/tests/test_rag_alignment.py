from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from uuid6 import uuid7

from app.core.config import Settings
from app.retrieval import pipeline as search_pipeline
from app.retrieval.coverage import CoverageSelectionResult
from app.retrieval.dense import DenseSearchHit
from app.retrieval.query_decomposition import QueryPlan
from app.services import grounded_answer


@pytest.mark.asyncio
async def test_settings_entry_forwards_the_complete_rag_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_stream(*args: object, **kwargs: object) -> AsyncIterator[object]:
        captured.update(kwargs)
        if False:
            yield None

    monkeypatch.setattr(grounded_answer, "stream_answer_with_citations", fake_stream)
    settings = Settings(
        refusal_score_gate_source="fusion",
        query_decomposition_enabled=True,
        coverage_selection_enabled=True,
        coverage_rank_cutoff=7,
        rerank_enabled=True,
        lexical_rrf_enabled=False,
        document_cap_per_version=3,
        rerank_candidate_k=37,
        answer_max_evidence_chars=9876,
    )

    items = [
        item
        async for item in grounded_answer.stream_answer_with_settings(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            query="question",
            top_k=7,
            settings=settings,
            chunk_strategy="semantic",
        )
    ]

    assert items == []
    assert captured["refusal_score_gate_source"] == "fusion"
    assert captured["query_decomposition_enabled"] is True
    assert captured["coverage_selection_enabled"] is True
    assert captured["coverage_rank_cutoff"] == 7
    assert captured["rerank_enabled"] is True
    assert captured["lexical_rrf_enabled"] is False
    assert captured["document_cap_per_version"] == 3
    assert captured["rerank_candidate_k"] == 37
    assert captured["max_evidence_chars"] == 9876
    assert captured["chunk_strategy"] == "semantic"


@pytest.mark.asyncio
async def test_candidate_pool_depth_does_not_depend_on_rerank_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[tuple[str, int]] = []
    hit = DenseSearchHit(
        chunk_id=uuid7(),
        document_id=uuid7(),
        version_id=uuid7(),
        version_no=1,
        title="doc",
        source_uri="doc.md",
        content="evidence",
        score=0.8,
        dense_score=0.8,
        heading_path=[],
        blocks=[],
    )

    async def fake_dense(*args: object, **kwargs: object) -> list[DenseSearchHit]:
        requested.append(("dense", cast(int, kwargs["top_k"])))
        return [hit]

    async def fake_lexical(*args: object, **kwargs: object) -> list[DenseSearchHit]:
        requested.append(("lexical", cast(int, kwargs["top_k"])))
        return []

    monkeypatch.setattr(search_pipeline, "multi_query_dense_search", fake_dense)
    monkeypatch.setattr(search_pipeline, "lexical_search", fake_lexical)

    result = await grounded_answer.answer_with_citations(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        query="question",
        top_k=5,
        rerank_enabled=False,
        rerank_candidate_k=50,
        lexical_rrf_enabled=True,
        refusal_score_gate_source="disabled",
    )

    assert requested == [("dense", 50), ("lexical", 50)]
    assert result.refused is True
    assert result.refusal_reason == "no_evidence"
    assert result.retrieved_chunks == 1
    assert result.score_source == "fusion"
    assert result.score_threshold_applied is False


@pytest.mark.asyncio
async def test_decomposed_query_uses_coverage_selector_and_reports_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hit = DenseSearchHit(
        chunk_id=uuid7(),
        document_id=uuid7(),
        version_id=uuid7(),
        version_no=1,
        title="selected",
        source_uri="selected.md",
        content="evidence",
        score=0.8,
        fusion_score=0.04,
        heading_path=[],
        blocks=[],
    )

    async def fake_plan(*args: object, **kwargs: object) -> QueryPlan:
        return QueryPlan(
            queries=["original", "requirement one", "requirement two"],
            decomposed=True,
            reason="two independent requirements",
            model="planner",
            provider="local",
        )

    async def fake_coverage(*args: object, **kwargs: object) -> CoverageSelectionResult:
        return CoverageSelectionResult(
            hits=[hit],
            applied=True,
            requirement_count=2,
            covered_requirement_count=2,
            candidate_count=17,
            lexical_candidate_count=11,
            reason="covered 2/2",
        )

    monkeypatch.setattr(search_pipeline.SearchPipeline, "_build_query_plan", fake_plan)
    monkeypatch.setattr(search_pipeline, "coverage_aware_hybrid_search", fake_coverage)

    result = await grounded_answer.answer_with_citations(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        query="original",
        top_k=5,
        query_decomposition_enabled=True,
        coverage_selection_enabled=True,
        rerank_enabled=False,
        refusal_score_gate_source="disabled",
    )

    assert result.refused is True
    assert result.coverage_selection_applied is True
    assert result.coverage_requirement_count == 2
    assert result.coverage_covered_requirement_count == 2
    assert result.coverage_candidate_count == 17
    assert result.coverage_reason == "covered 2/2"
