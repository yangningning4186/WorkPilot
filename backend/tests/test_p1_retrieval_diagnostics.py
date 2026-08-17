from uuid import UUID

import pytest

from app.retrieval.dense import DenseSearchHit
from eval.p1_retrieval_diagnostics import (
    DeepRow,
    _document_rank,
    _p1a_status,
    blend_rrf_ce_ranks,
)


def _hit(number: int) -> DenseSearchHit:
    return DenseSearchHit(
        chunk_id=UUID(f"00000000-0000-0000-0000-{number:012d}"),
        document_id=UUID("10000000-0000-0000-0000-000000000001"),
        version_id=UUID("20000000-0000-0000-0000-000000000001"),
        version_no=1,
        title="doc",
        source_uri="test://doc",
        content=str(number),
        score=1.0,
        heading_path=[],
        blocks=[],
    )


def test_rank_blend_respects_both_endpoints() -> None:
    rrf = [_hit(1), _hit(2), _hit(3)]
    ce = list(reversed(rrf))

    assert [hit.chunk_id for hit in blend_rrf_ce_ranks(rrf, ce, ce_weight=0)] == [
        hit.chunk_id for hit in rrf
    ]
    assert [hit.chunk_id for hit in blend_rrf_ce_ranks(rrf, ce, ce_weight=1)] == [
        hit.chunk_id for hit in ce
    ]
    with pytest.raises(ValueError, match="ce_weight"):
        blend_rrf_ce_ranks(rrf, ce, ce_weight=1.1)


def test_p1a_status_distinguishes_strict_demotion_and_not_rescued() -> None:
    assert (
        _p1a_status(
            dense_rank=1,
            lexical_rank=None,
            rrf_rank=3,
            union_rank=3,
            old_rank=20,
            final_top_k=10,
        )
        == "rerank_strict_demoted"
    )
    assert (
        _p1a_status(
            dense_rank=20,
            lexical_rank=None,
            rrf_rank=30,
            union_rank=30,
            old_rank=40,
            final_top_k=10,
        )
        == "rerank_not_rescued"
    )


def test_document_rank_counts_unique_versions_not_chunk_positions() -> None:
    left = UUID("30000000-0000-0000-0000-000000000001")
    right = UUID("30000000-0000-0000-0000-000000000002")
    rows = [
        DeepRow(_hit(rank).chunk_id, version_id, 0, 10, rank)
        for rank, version_id in enumerate([left, left, left, right], start=1)
    ]

    assert _document_rank(rows, left) == 1
    assert _document_rank(rows, right) == 2
