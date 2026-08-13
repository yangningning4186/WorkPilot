import pytest
from uuid6 import uuid7

from app.retrieval.citations import (
    REFUSAL_TEXT,
    CitationValidationError,
    build_evidence_segments,
    parse_citations,
)
from app.retrieval.dense import DenseSearchHit


def _hit() -> DenseSearchHit:
    version_id = uuid7()
    document_id = uuid7()
    return DenseSearchHit(
        chunk_id=uuid7(),
        document_id=document_id,
        version_id=version_id,
        version_no=1,
        title="Dense Retrieval",
        source_uri="dense.md",
        content="# Dense\n\nVector similarity",
        score=0.9,
        heading_path=["Dense"],
        blocks=[
            {
                "block_id": str(uuid7()),
                "block_idx": 0,
                "block_type": "title",
                "text": "# Dense",
                "char_start": 0,
                "char_end": 7,
                "heading_path": ["Dense"],
                "locations": [],
            },
            {
                "block_id": str(uuid7()),
                "block_idx": 1,
                "block_type": "paragraph",
                "text": "Vector similarity",
                "char_start": 9,
                "char_end": 26,
                "heading_path": ["Dense"],
                "locations": [],
            },
        ],
    )


def test_evidence_is_block_anchored_and_respects_character_budget() -> None:
    hit = _hit()
    segments = build_evidence_segments([hit, hit], max_chars=6)

    assert len(segments) == 1
    assert segments[0].citation_id == "S1"
    assert segments[0].quote == "Vector"
    assert segments[0].char_start == 9
    assert segments[0].char_end == 15


def test_parse_citations_maps_and_deduplicates_labels() -> None:
    evidence = build_evidence_segments([_hit()], max_chars=100)
    citations = parse_citations("A fact [S1]. Repeated [S1].", evidence)

    assert [citation.citation_id for citation in citations] == ["S1"]
    assert citations[0].block_id == evidence[0].block_id


def test_unknown_or_missing_citation_fails_closed() -> None:
    evidence = build_evidence_segments([_hit()], max_chars=100)

    with pytest.raises(CitationValidationError, match="S9") as unknown:
        parse_citations("Unsupported [S9]", evidence)
    assert unknown.value.unknown_ids == ["S9"]

    with pytest.raises(CitationValidationError) as missing:
        parse_citations("No marker", evidence)
    assert missing.value.missing is True


def test_exact_refusal_does_not_require_citation() -> None:
    assert parse_citations(REFUSAL_TEXT, []) == []
