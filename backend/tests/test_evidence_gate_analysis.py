from uuid6 import uuid7

from app.rag.retrieval.dense import DenseSearchHit
from eval.evidence_gate_analysis import _classify, _minimum_full_visibility_chars


def test_false_refusal_classification_keeps_pipeline_stages_distinct() -> None:
    assert (
        _classify(
            refusal_reason="evidence_gate_invalid",
            retrieval_statuses={0: "hit"},
            gate_coverage=[1.0],
            answer_coverage=[1.0],
        )
        == "gate_response_invalid"
    )


def test_minimum_full_visibility_chars_finds_sequential_budget_boundary() -> None:
    version_id = uuid7()
    block_id = uuid7()
    hit = DenseSearchHit(
        chunk_id=uuid7(),
        document_id=uuid7(),
        version_id=version_id,
        version_no=1,
        title="doc",
        source_uri="doc.md",
        content="0123456789",
        score=1.0,
        heading_path=[],
        blocks=[
            {
                "block_id": str(block_id),
                "block_type": "paragraph",
                "text": "0123456789",
                "char_start": 100,
                "char_end": 110,
                "heading_path": [],
                "locations": [],
            }
        ],
    )
    span = {"version_id": str(version_id), "char_start": 104, "char_end": 108}

    assert _minimum_full_visibility_chars([hit], [span], max_chars=10) == 8
    assert _minimum_full_visibility_chars([hit], [span], max_chars=7) is None
    assert (
        _classify(
            refusal_reason="model_insufficient_evidence",
            retrieval_statuses={0: "miss"},
            gate_coverage=[0.0],
            answer_coverage=[0.0],
        )
        == "retrieval_miss"
    )
    assert (
        _classify(
            refusal_reason="model_insufficient_evidence",
            retrieval_statuses={0: "hit"},
            gate_coverage=[1.0, 0.0],
            answer_coverage=[1.0, 0.0],
        )
        == "evidence_budget_miss"
    )
    assert (
        _classify(
            refusal_reason="model_insufficient_evidence",
            retrieval_statuses={0: "hit"},
            gate_coverage=[0.5],
            answer_coverage=[1.0],
        )
        == "gate_packing_miss"
    )
    assert (
        _classify(
            refusal_reason="model_insufficient_evidence",
            retrieval_statuses={0: "hit"},
            gate_coverage=[1.0],
            answer_coverage=[1.0],
        )
        == "gate_model_false_negative"
    )
