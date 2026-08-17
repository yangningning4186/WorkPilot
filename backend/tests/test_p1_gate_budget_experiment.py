from eval.p1_gate_budget_experiment import _summarize


def test_gate_budget_summary_keeps_visibility_and_decision_separate() -> None:
    rows = [
        {
            "budget": 6000,
            "gold_fully_visible": False,
            "sufficient": True,
            "input_tokens": 100,
            "output_tokens": 10,
            "latency_ms": 200,
        },
        {
            "budget": 8000,
            "gold_fully_visible": True,
            "sufficient": True,
            "input_tokens": 150,
            "output_tokens": 12,
            "latency_ms": 260,
        },
    ]

    summary = _summarize(rows, budgets=(6000, 8000))

    assert summary["6000"]["fully_visible"] == 0
    assert summary["6000"]["sufficient"] == 1
    assert summary["8000"]["input_tokens_mean"] == 150
