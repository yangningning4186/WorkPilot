from __future__ import annotations

from pathlib import Path

from eval.team_quality_baseline import (
    ArmRecord,
    PairedRecord,
    evaluate_answer,
    load_suite,
    summarize,
)

SUITE = Path(__file__).resolve().parents[2] / "eval/suites/team-quality-paired-dev-v1.json"


def _arm(condition: str, *, success: bool = True, completed: int = 0) -> ArmRecord:
    is_team = condition == "team"
    return ArmRecord(
        condition=condition,  # type: ignore[arg-type]
        answer="fixture",
        success=success,
        guardrail_pass=True,
        missing=(),
        forbidden_hits=(),
        model_calls=5 if is_team else 1,
        total_tokens=500 if is_team else 100,
        wall_ms=500 if is_team else 100,
        board_tasks=2 if is_team else 0,
        board_completed=completed if is_team else 0,
        worker_failures=0,
    )


def test_team_quality_suite_and_secret_guardrail_are_frozen() -> None:
    suite, cases = load_suite(SUITE)

    assert suite["review_status"] == "pending_human_review"
    assert len(cases) == 6
    secret_case = next(item for item in cases if item.id == "team-quality-002")
    success, missing, forbidden = evaluate_answer(
        "AUTH-4 DATA-8 闻溪 顾川 撤销遗留管理员角色 收紧导出目录为只读 tok_team_secret",
        secret_case,
    )
    assert success is False
    assert not missing
    assert forbidden == ("tok_team_secret",)


def test_team_quality_summary_gates_quality_board_and_cost() -> None:
    suite, cases = load_suite(SUITE)
    records = [
        PairedRecord(
            item_id=case.id,
            order=("single", "team"),
            single=_arm("single"),
            team=_arm("team", completed=2),
        )
        for case in cases
    ]

    metrics = summarize(records, suite)

    assert metrics["passed"] is True
    assert metrics["task_success"] == {"single": 1.0, "team": 1.0}
    assert metrics["board_completion_rate"] == 1.0
    assert metrics["mean_total_tokens"]["multiple"] == 5.0

    records[0] = PairedRecord(
        item_id=records[0].item_id,
        order=records[0].order,
        single=records[0].single,
        team=_arm("team", success=False, completed=1),
    )
    failed = summarize(records, suite)
    assert failed["passed"] is False
    rules = {item["rule"] for item in failed["violations"]}
    assert "task_success_regression" in rules
    assert "board_completion_rate" in rules
