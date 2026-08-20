from uuid import UUID

from uuid6 import uuid7

from app.rag.retrieval.coverage import coverage_aware_top_k
from app.rag.retrieval.dense import DenseSearchHit


def _hit(name: str, *, chunk_id: UUID | None = None) -> DenseSearchHit:
    return DenseSearchHit(
        chunk_id=chunk_id or uuid7(),
        document_id=uuid7(),
        version_id=uuid7(),
        version_no=1,
        title=name,
        source_uri=f"{name}.md",
        content=name,
        score=0.5,
        dense_score=0.5,
        heading_path=[],
        blocks=[],
    )


def test_coverage_selector_keeps_complementary_subquery_hits() -> None:
    noise = _hit("noise")
    left = _hit("left")
    right = _hit("right")

    result = coverage_aware_top_k(
        [noise, left, right],
        [[left, noise], [right, noise]],
        top_k=2,
        rank_cutoff=1,
    )

    assert [hit.chunk_id for hit in result.hits] == [left.chunk_id, right.chunk_id]
    assert result.applied is True
    assert result.covered_requirement_count == 2


def test_repeated_generic_candidate_does_not_consume_two_requirement_slots() -> None:
    shared = _hit("shared")
    original = _hit("original")
    specific = _hit("specific")

    result = coverage_aware_top_k(
        [original, shared, specific],
        [[shared], [shared, specific]],
        top_k=2,
        rank_cutoff=2,
    )

    assert [hit.chunk_id for hit in result.hits] == [shared.chunk_id, specific.chunk_id]
    assert result.covered_requirement_count == 2


def test_selector_is_exact_noop_without_real_subqueries() -> None:
    hits = [_hit("first"), _hit("second")]

    result = coverage_aware_top_k(hits, [], top_k=1)

    assert result.hits == hits[:1]
    assert result.applied is False
    assert result.reason.startswith("查询未分解")
