from uuid import UUID

from app.retrieval.dense import DenseSearchHit
from eval.p1_document_two_hop_experiment import _grid_candidates, _rank_documents


def _hit(number: int, version: int) -> DenseSearchHit:
    return DenseSearchHit(
        chunk_id=UUID(f"00000000-0000-0000-0000-{number:012d}"),
        document_id=UUID(f"10000000-0000-0000-0000-{version:012d}"),
        version_id=UUID(f"20000000-0000-0000-0000-{version:012d}"),
        version_no=1,
        title=f"doc-{version}",
        source_uri="test://doc",
        content="evidence",
        score=1.0,
        heading_path=[],
        blocks=[],
    )


def test_document_ranking_uses_first_chunk_per_arm() -> None:
    dense = [_hit(1, 1), _hit(2, 1), _hit(3, 2)]
    lexical = [_hit(4, 2), _hit(5, 1)]

    ranked = _rank_documents(dense, lexical, rrf_k=60)

    assert [hit.version_id for hit in ranked] == [dense[0].version_id, dense[2].version_id]


def test_grid_candidates_respects_document_and_total_budget() -> None:
    versions = [UUID(f"20000000-0000-0000-0000-{index:012d}") for index in range(1, 6)]
    local = {
        version: [_hit(version_index * 100 + chunk, version_index) for chunk in range(25)]
        for version_index, version in enumerate(versions, start=1)
    }

    candidates = _grid_candidates(
        versions, local, document_top_m=5, local_top_n=20
    )

    assert len(candidates) == 100
    assert len({hit.version_id for hit in candidates}) == 5
