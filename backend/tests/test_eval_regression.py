"""统一回归层的契约测试；全部使用合成报告，不调用模型或真实工具。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from eval.regression import (
    RegressionRefused,
    build_baseline,
    evaluate_regression,
    load_normalized_report,
    load_policy,
    main,
)

POLICY = Path(__file__).resolve().parents[2] / "eval/policies/cowork.json"


def _cowork_item(
    case_id: str,
    *,
    success: bool = True,
    guardrail: bool = True,
    tokens: int = 100,
    error: str | None = None,
    reading: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "item_id": case_id,
        "split": "dev",
        "category": "reading" if reading is not None else "workspace",
        "prompt": "PRIVATE PROMPT",
        "observation": {
            "status": "runner_error" if error else "done",
            "error": error,
            "response": "PRIVATE ANSWER",
            "used_tokens": tokens,
            "used_calls": 2,
            "workspace": "/Users/private/project",
            "tool_trace": [
                {
                    "name": "read_text_file",
                    "status": "ok",
                    "arguments": {"path": "/Users/private/project/secret.txt"},
                    "result": {"content": "PRIVATE SOURCE"},
                }
            ],
        },
        "score": {
            "task_success": success,
            "status_match": success,
            "assertions_pass": success,
            "guardrail_pass": guardrail,
            "tool_selection": {"passed": success},
            "within_tool_budget": True,
            "step_efficiency": 1.0,
            "reading": reading,
        },
    }


def _cowork_report(
    items: list[dict[str, Any]],
    *,
    label: str,
    suite_sha: str = "a" * 64,
    model: str = "fixture-model",
) -> dict[str, Any]:
    return {
        "schema_version": "cowork-eval-report.v1",
        "suite": "cowork-fixture",
        "suite_version": "1.0.0",
        "manifest": {
            "label": label,
            "suite_sha256": suite_sha,
            "suite_origin": "synthetic",
            "suite_review_status": "approved",
            "suite_reviewer": "fixture-owner",
            "suite_reviewed_at": "2026-08-24T00:00:00+00:00",
            "item_ids": [item["item_id"] for item in items],
            "model": {
                "provider": "fixture",
                "model": model,
                "endpoint": "local",
                "mode": "evaluation",
            },
            "budgets": {"tokens": 10_000, "calls": 20, "wall_ms": 60_000},
            "fixture_policy": {"network": "disabled"},
            "reproducibility": {"git_sha": "f" * 40, "git_dirty": False},
        },
        "metrics": {},
        "items": items,
    }


def _write(tmp_path: Path, name: str, payload: dict[str, Any]) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _snapshot(tmp_path: Path, report: dict[str, Any], name: str = "baseline") -> Path:
    source = load_normalized_report(_write(tmp_path, f"{name}-source", report))
    payload = build_baseline(source, load_policy(POLICY))
    return _write(tmp_path, name, payload)


def test_cowork_snapshot_contains_only_metrics_and_hashes(tmp_path: Path) -> None:
    report = _cowork_report([_cowork_item("case-1")], label="baseline")
    baseline = json.loads(_snapshot(tmp_path, report).read_text(encoding="utf-8"))

    encoded = json.dumps(baseline, ensure_ascii=False)
    for private in (
        "PRIVATE PROMPT",
        "PRIVATE ANSWER",
        "PRIVATE SOURCE",
        "/Users/private/project",
        "secret.txt",
    ):
        assert private not in encoded
    assert baseline["cases"][0]["metrics"]["task_success"] == {
        "numerator": 1.0,
        "denominator": 1.0,
    }
    assert baseline["git_dirty"] is False
    assert baseline["review"] == {
        "origin": "synthetic",
        "status": "approved",
        "reviewer": "fixture-owner",
        "reviewed_at": "2026-08-24T00:00:00+00:00",
    }
    assert baseline["selection"] == {"split_counts": {"dev": 1}}
    assert len(baseline["integrity"]["value"]) == 64


def test_identical_cowork_report_passes(tmp_path: Path) -> None:
    report = _cowork_report(
        [_cowork_item("a"), _cowork_item("b", success=False)],
        label="candidate",
    )
    baseline = load_normalized_report(_snapshot(tmp_path, report))
    candidate = load_normalized_report(_write(tmp_path, "candidate", report))

    outcome = evaluate_regression(baseline, candidate, load_policy(POLICY))

    assert outcome.passed
    assert not outcome.violations


def test_candidate_from_dirty_worktree_is_not_comparable(tmp_path: Path) -> None:
    report = _cowork_report([_cowork_item("a")], label="baseline")
    baseline = load_normalized_report(_snapshot(tmp_path, report))
    candidate_report = deepcopy(report)
    candidate_report["label"] = "candidate"
    candidate_report["manifest"]["label"] = "candidate"
    candidate_report["manifest"]["reproducibility"]["git_dirty"] = True
    candidate = load_normalized_report(_write(tmp_path, "dirty-candidate", candidate_report))

    with pytest.raises(RegressionRefused, match="Git clean"):
        evaluate_regression(baseline, candidate, load_policy(POLICY))


def test_case_regression_blocks_even_when_aggregate_is_unchanged(tmp_path: Path) -> None:
    baseline_report = _cowork_report(
        [_cowork_item("a"), _cowork_item("b", success=False)],
        label="baseline",
    )
    candidate_report = _cowork_report(
        [_cowork_item("a", success=False), _cowork_item("b")],
        label="candidate",
    )
    baseline = load_normalized_report(_snapshot(tmp_path, baseline_report))
    candidate = load_normalized_report(_write(tmp_path, "candidate", candidate_report))

    outcome = evaluate_regression(baseline, candidate, load_policy(POLICY))

    assert not outcome.passed
    violation = next(item for item in outcome.violations if item.rule == "case_regression")
    assert violation.metric == "task_success"
    assert violation.case_ids == ("a",)


def test_guardrail_is_a_hard_invariant(tmp_path: Path) -> None:
    baseline_report = _cowork_report([_cowork_item("a")], label="baseline")
    candidate_report = _cowork_report(
        [_cowork_item("a", guardrail=False)],
        label="candidate",
    )
    outcome = evaluate_regression(
        load_normalized_report(_snapshot(tmp_path, baseline_report)),
        load_normalized_report(_write(tmp_path, "candidate", candidate_report)),
        load_policy(POLICY),
    )

    assert any(
        item.metric == "guardrail_pass" and item.rule in {"aggregate_regression", "minimum"}
        for item in outcome.violations
    )


@pytest.mark.parametrize(
    ("tokens", "passed"),
    [(120, True), (121, False)],
)
def test_token_cost_allows_at_most_twenty_percent(
    tmp_path: Path, tokens: int, passed: bool
) -> None:
    baseline_report = _cowork_report([_cowork_item("a", tokens=100)], label="baseline")
    candidate_report = _cowork_report(
        [_cowork_item("a", tokens=tokens)],
        label="candidate",
    )
    outcome = evaluate_regression(
        load_normalized_report(_snapshot(tmp_path, baseline_report, name=f"base-{tokens}")),
        load_normalized_report(_write(tmp_path, f"candidate-{tokens}", candidate_report)),
        load_policy(POLICY),
    )

    assert outcome.passed is passed


def test_config_drift_requires_an_explicit_override(tmp_path: Path) -> None:
    baseline_report = _cowork_report([_cowork_item("a")], label="baseline")
    candidate_report = _cowork_report(
        [_cowork_item("a")],
        label="candidate",
        model="other-model",
    )
    baseline = load_normalized_report(_snapshot(tmp_path, baseline_report))
    candidate = load_normalized_report(_write(tmp_path, "candidate", candidate_report))
    policy = load_policy(POLICY)

    with pytest.raises(RegressionRefused, match="配置 fingerprint"):
        evaluate_regression(baseline, candidate, policy)
    assert evaluate_regression(
        baseline,
        candidate,
        policy,
        allow_config_drift=True,
    ).passed


def test_suite_fingerprint_and_case_set_must_match(tmp_path: Path) -> None:
    baseline_report = _cowork_report([_cowork_item("a")], label="baseline")
    policy = load_policy(POLICY)
    baseline = load_normalized_report(_snapshot(tmp_path, baseline_report))

    drifted_suite = _cowork_report(
        [_cowork_item("a")],
        label="candidate",
        suite_sha="b" * 64,
    )
    with pytest.raises(RegressionRefused, match="suite fingerprint"):
        evaluate_regression(
            baseline,
            load_normalized_report(_write(tmp_path, "drifted-suite", drifted_suite)),
            policy,
        )

    changed_cases = _cowork_report(
        [_cowork_item("a"), _cowork_item("b")],
        label="candidate",
    )
    with pytest.raises(RegressionRefused, match="case_id 集合"):
        evaluate_regression(
            baseline,
            load_normalized_report(_write(tmp_path, "changed-cases", changed_cases)),
            policy,
        )


def test_optional_metric_cannot_disappear_from_only_one_arm(tmp_path: Path) -> None:
    reading = {
        "read_before_claim": {"passed": 1, "total": 1},
        "quote_verifiability": {"passed": 1, "total": 1},
        "locator_accuracy": {"passed": 1, "total": 1},
    }
    baseline_report = _cowork_report(
        [_cowork_item("a", reading=reading)],
        label="baseline",
    )
    candidate_item = _cowork_item("a")
    candidate_item["category"] = "reading"
    candidate_report = _cowork_report([candidate_item], label="candidate")

    with pytest.raises(RegressionRefused, match="适用样本发生漂移"):
        evaluate_regression(
            load_normalized_report(_snapshot(tmp_path, baseline_report)),
            load_normalized_report(_write(tmp_path, "candidate", candidate_report)),
            load_policy(POLICY),
        )


def test_candidate_runner_error_is_quality_violation_not_refusal(tmp_path: Path) -> None:
    baseline_report = _cowork_report([_cowork_item("a")], label="baseline")
    candidate_report = _cowork_report(
        [_cowork_item("a", success=False, error="timeout")],
        label="candidate",
    )
    outcome = evaluate_regression(
        load_normalized_report(_snapshot(tmp_path, baseline_report)),
        load_normalized_report(_write(tmp_path, "candidate", candidate_report)),
        load_policy(POLICY),
    )

    assert not outcome.passed
    assert any(
        item.rule == "candidate_error" and item.case_ids == ("a",) for item in outcome.violations
    )


def test_tampered_baseline_is_rejected(tmp_path: Path) -> None:
    path = _snapshot(
        tmp_path,
        _cowork_report([_cowork_item("a")], label="baseline"),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["cases"][0]["metrics"]["task_success"]["numerator"] = 0
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegressionRefused, match="完整性校验失败"):
        load_normalized_report(path)


def test_baseline_is_bound_to_the_reviewed_policy(tmp_path: Path) -> None:
    report = _cowork_report([_cowork_item("a")], label="baseline")
    baseline = load_normalized_report(_snapshot(tmp_path, report))
    candidate = load_normalized_report(_write(tmp_path, "candidate", report))
    changed_policy_payload = json.loads(POLICY.read_text(encoding="utf-8"))
    changed_policy_payload["metrics"][0]["max_absolute_regression"] = 0.01
    changed_policy_path = _write(tmp_path, "changed-policy", changed_policy_payload)

    with pytest.raises(RegressionRefused, match="policy 与当前 policy 不一致"):
        evaluate_regression(
            baseline,
            candidate,
            load_policy(changed_policy_path),
        )


def test_cli_exit_codes_distinguish_regression_and_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline_report = _cowork_report([_cowork_item("a")], label="baseline")
    baseline = _snapshot(tmp_path, baseline_report)
    regressed = _write(
        tmp_path,
        "regressed",
        _cowork_report([_cowork_item("a", success=False)], label="candidate"),
    )
    incompatible = _write(
        tmp_path,
        "incompatible",
        _cowork_report(
            [_cowork_item("a")],
            label="candidate",
            suite_sha="b" * 64,
        ),
    )

    assert main(["check", str(regressed), "--baseline", str(baseline)]) == 1
    assert main(["check", str(incompatible), "--baseline", str(baseline)]) == 2
    assert "拒绝判定" in capsys.readouterr().err


def test_retrieval_and_generation_reports_share_the_same_normalizer(tmp_path: Path) -> None:
    retrieval = {
        "kind": "retrieval",
        "dataset": "kb-dev",
        "label": "r",
        "config": {"dataset_fingerprint": "f" * 64, "top_k": 1},
        "config_hash": "c" * 64,
        "items": [
            {
                "item_id": "r1",
                "category": "single_hop",
                "answerable": True,
                "error": None,
                "latency_ms": 1,
                "top_score": 0.8,
                "retrieval": {
                    "span_recall_at_k": 1,
                    "budget_span_recall": 1,
                    "ndcg_at_k": 1,
                    "alpha_ndcg_at_k": 1,
                    "mrr": 1,
                    "context_precision": 1,
                },
                "retrieved": [
                    {
                        "version_id": "v1",
                        "char_start": 0,
                        "char_end": 10,
                        "content_tokens": 5,
                    }
                ],
            }
        ],
    }
    generation = {
        "kind": "generation",
        "dataset": "gen-dev",
        "label": "g",
        "config": {"dataset_fingerprint": "e" * 64},
        "config_hash": "d" * 64,
        "items": [
            {
                "item_id": "g1",
                "category": "single_hop",
                "answerable": True,
                "error": None,
                "latency_ms": 1,
                "citations": [{"citation_id": "S1"}],
                "refused": False,
                "refusal_correct": True,
                "citation_validity": {"valid": True},
                "constraint_pass": {"passed": True},
                "citation_gold_alignment": {"aligned": 1, "total": 1},
                "total_tokens": 10,
            }
        ],
    }

    normalized_retrieval = load_normalized_report(_write(tmp_path, "retrieval", retrieval))
    normalized_generation = load_normalized_report(_write(tmp_path, "generation", generation))

    assert normalized_retrieval.kind == "retrieval"
    assert normalized_retrieval.cases[0].metrics["span_recall_at_k"].value == 1
    assert normalized_generation.kind == "generation"
    assert normalized_generation.cases[0].metrics["citation_gold_alignment"].value == 1


def test_snapshot_refuses_reports_with_errors(tmp_path: Path) -> None:
    report = _cowork_report(
        [_cowork_item("a", success=False, error="timeout")],
        label="failed",
    )
    normalized = load_normalized_report(_write(tmp_path, "failed", report))

    with pytest.raises(RegressionRefused, match="不能晋升 baseline"):
        build_baseline(normalized, load_policy(POLICY))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda report: report["manifest"]["reproducibility"].update(git_dirty=True),
            "Git dirty",
        ),
        (
            lambda report: report["manifest"].update(suite_review_status="pending_human_review"),
            "题库未 approved",
        ),
        (
            lambda report: report["manifest"].update(suite_reviewer=None),
            "题库未 approved",
        ),
    ],
)
def test_snapshot_refuses_dirty_or_unapproved_reports(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    report = _cowork_report([_cowork_item("a")], label="blocked")
    mutation(report)
    normalized = load_normalized_report(_write(tmp_path, "blocked", report))

    with pytest.raises(RegressionRefused, match=message):
        build_baseline(normalized, load_policy(POLICY))


def test_policy_file_is_immutable_input_to_the_baseline(tmp_path: Path) -> None:
    report = _cowork_report([_cowork_item("a")], label="baseline")
    normalized = load_normalized_report(_write(tmp_path, "source", report))
    policy = load_policy(POLICY)
    baseline = build_baseline(normalized, policy)

    assert baseline["policy"] == {"name": policy.name, "sha256": policy.sha256}
    assert deepcopy(baseline)["integrity"] == baseline["integrity"]


def test_raw_report_cannot_be_used_as_a_baseline(tmp_path: Path) -> None:
    report = _cowork_report([_cowork_item("a")], label="raw")
    raw = load_normalized_report(_write(tmp_path, "raw", report))

    with pytest.raises(RegressionRefused, match=r"eval\.regression snapshot"):
        evaluate_regression(raw, raw, load_policy(POLICY))


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    report = tmp_path / "duplicate-report.json"
    report.write_text('{"kind":"cowork","kind":"generation"}', encoding="utf-8")
    with pytest.raises(RegressionRefused, match="重复 JSON key"):
        load_normalized_report(report)

    policy = tmp_path / "duplicate-policy.json"
    policy.write_text(
        '{"schema_version":"workpilot-regression-policy.v1",'
        '"name":"a","name":"b","report_kind":"cowork","metrics":[]}',
        encoding="utf-8",
    )
    with pytest.raises(RegressionRefused, match="重复 JSON key"):
        load_policy(policy)


def test_snapshot_requires_reproducibility_provenance(tmp_path: Path) -> None:
    report = _cowork_report([_cowork_item("a")], label="missing-git")
    report["manifest"].pop("reproducibility")
    normalized = load_normalized_report(_write(tmp_path, "missing-git", report))

    with pytest.raises(RegressionRefused, match="Git SHA"):
        build_baseline(normalized, load_policy(POLICY))


def test_snapshot_cli_never_overwrites_an_existing_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _write(
        tmp_path,
        "source-for-cli",
        _cowork_report([_cowork_item("a")], label="approved"),
    )
    output = tmp_path / "baseline.json"

    assert main(["snapshot", str(report), "--output", str(output)]) == 0
    original = output.read_bytes()
    assert main(["snapshot", str(report), "--output", str(output)]) == 2

    assert output.read_bytes() == original
    assert "拒绝覆盖" in capsys.readouterr().err
