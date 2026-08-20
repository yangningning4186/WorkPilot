from uuid import UUID

from app.rag.retrieval.dense import DenseSearchHit
from eval.mapping import GoldSpan
from eval.rerank_truncation_experiment import _map_gold_candidates, _span_result


def _hit(*, content: str = "0123456789", char_start: int = 100) -> DenseSearchHit:
    return DenseSearchHit(
        chunk_id=UUID("00000000-0000-0000-0000-000000000001"),
        document_id=UUID("00000000-0000-0000-0000-000000000002"),
        version_id=UUID("00000000-0000-0000-0000-000000000003"),
        version_no=1,
        title="Title",
        source_uri="test://doc",
        content=content,
        score=1.0,
        heading_path=["Heading"],
        blocks=[],
        char_start=char_start,
        char_end=char_start + len(content),
    )


def test_gold_candidate_position_includes_title_and_heading_prefix() -> None:
    hit = _hit()
    span = GoldSpan(
        version_id=hit.version_id,
        char_start=103,
        char_end=106,
        quote="345",
    )

    mapped = _map_gold_candidates(
        [span], [hit], theta=0.5, text_mode="title_heading_content"
    )[0][0]

    assert mapped.char_start == len("Title\nHeading\n") + 3
    assert mapped.char_end == len("Title\nHeading\n") + 6
    assert mapped.rrf_rank == 1


def test_span_status_separates_client_server_and_visible_ranking_failures() -> None:
    hit = _hit()
    mapping = _map_gold_candidates(
        [GoldSpan(hit.version_id, 103, 106, "345")],
        [hit],
        theta=0.5,
        text_mode="content",
    )[0]

    client = _span_result(
        0,
        mapping,
        sent_lengths={"C1": 5},
        rank_by_id={"C1": 20},
        audit_by_key={},
        final_top_k=10,
    )
    server = _span_result(
        0,
        mapping,
        sent_lengths={"C1": 10},
        rank_by_id={"C1": 20},
        audit_by_key={
            ("C1", "S0"): {
                "total_tokens": 3,
                "visible_tokens": 2,
                "fully_visible": False,
            }
        },
        final_top_k=10,
    )
    ranking = _span_result(
        0,
        mapping,
        sent_lengths={"C1": 10},
        rank_by_id={"C1": 20},
        audit_by_key={
            ("C1", "S0"): {
                "total_tokens": 3,
                "visible_tokens": 3,
                "fully_visible": True,
            }
        },
        final_top_k=10,
    )

    assert client["status"] == "client_truncated"
    assert server["status"] == "server_truncated"
    assert ranking["status"] == "ranking_mismatch"
    assert ranking["demoted_from_top_k"] is True
