from uuid import UUID

from app.retrieval.dense import DenseSearchHit
from eval.p1_bm25_experiment import Bm25Document, Bm25Index


def _hit(number: int) -> DenseSearchHit:
    return DenseSearchHit(
        chunk_id=UUID(f"00000000-0000-0000-0000-{number:012d}"),
        document_id=UUID(f"10000000-0000-0000-0000-{number:012d}"),
        version_id=UUID(f"20000000-0000-0000-0000-{number:012d}"),
        version_no=1,
        title=f"doc-{number}",
        source_uri="test://doc",
        content="evidence",
        score=0,
        heading_path=[],
        blocks=[],
    )


def test_bm25_prefers_higher_tf_but_normalizes_document_length() -> None:
    documents = [Bm25Document(_hit(1), 10), Bm25Document(_hit(2), 100)]
    index = Bm25Index(documents, {"target": [(0, 2), (1, 3)]})

    ranked = index.search(["target"], top_k=2)

    assert [hit.chunk_id for hit in ranked] == [documents[0].hit.chunk_id, documents[1].hit.chunk_id]
    assert ranked[0].lexical_score is not None


def test_bm25_returns_no_candidates_without_term_overlap() -> None:
    index = Bm25Index([Bm25Document(_hit(1), 10)], {"known": [(0, 1)]})

    assert index.search(["unknown"], top_k=10) == []
