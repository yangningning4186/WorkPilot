from eval.evidence_gate_analysis import _classify


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
