from uuid import UUID

from uuid6 import uuid7

from app.rag.retrieval.dense import DenseSearchHit
from eval.mapping import GoldSpan
from eval.p1_top5_recovery_experiment import _gold_coverage_top_k


def _hit(version_id: UUID, start: int, end: int) -> DenseSearchHit:
    return DenseSearchHit(
        chunk_id=uuid7(),
        document_id=uuid7(),
        version_id=version_id,
        version_no=1,
        title="doc",
        source_uri="doc.md",
        content="x" * (end - start),
        score=1.0,
        heading_path=[],
        blocks=[],
        char_start=start,
        char_end=end,
    )


def test_gold_coverage_oracle_prefers_complementary_chunks() -> None:
    version_id = uuid7()
    noise = _hit(version_id, 0, 10)
    second = _hit(version_id, 200, 300)
    first = _hit(version_id, 100, 200)
    spans = [
        GoldSpan(version_id=version_id, char_start=120, char_end=150, quote="a"),
        GoldSpan(version_id=version_id, char_start=220, char_end=250, quote="b"),
    ]

    selected = _gold_coverage_top_k([noise, second, first], spans, top_k=2)

    assert [hit.chunk_id for hit in selected] == [second.chunk_id, first.chunk_id]
