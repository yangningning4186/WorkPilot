from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from eval.catalog import (
    BASELINE_SCHEMA_VERSION,
    CATALOG_SCHEMA_VERSION,
    canonical_json,
    doctor_catalog,
    main,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = REPO_ROOT / "eval/catalog.json"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _fixture_tree(root: Path, *, baseline_status: str = "rebuild_required") -> dict[str, Any]:
    suite = {
        "name": "suite-v1",
        "origin": "synthetic",
        "review_status": "approved",
        "reviewer": "fixture-owner",
        "reviewed_at": "2026-08-24T00:00:00+00:00",
        "items": [{"id": "case-1", "split": "dev"}],
    }
    policy = {
        "schema_version": "workpilot-regression-policy.v1",
        "name": "cowork-policy-v1",
        "report_kind": "cowork",
        "metrics": [{"name": "task_success", "direction": "higher"}],
    }
    replay = {
        "schema": "workpilot.run-replay-bundle",
        "schema_version": 1,
        "cases": [{"case_id": "case-1"}],
    }
    _write_json(root / "suites/suite.json", suite)
    _write_json(root / "policies/cowork.json", policy)
    _write_json(root / "replays/events.json", replay)
    _write_json(root / "baselines/cowork.json", {"legacy": True})
    baseline: dict[str, Any] = {
        "status": baseline_status,
        "path": "baselines/cowork.json",
    }
    if baseline_status == "rebuild_required":
        baseline["reason"] = "legacy runner report"
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "tracks": [
            {
                "id": "cowork-core",
                "kind": "cowork",
                "suite": "suites/suite.json",
                "policy": "policies/cowork.json",
                "selection": {"split": "dev", "item_count": 1},
                "baseline": baseline,
            }
        ],
        "replay_suites": [
            {
                "id": "run-protocol-v1",
                "kind": "event",
                "mode": "offline_validation_only",
                "path": "replays/events.json",
            }
        ],
    }


def _doctor(tmp_path: Path, catalog: dict[str, Any]):
    path = tmp_path / "catalog.json"
    _write_json(path, catalog)
    return doctor_catalog(path, repo_root=tmp_path)


def _codes(report) -> set[str]:
    return {issue.code for issue in report.issues}


def _promote_valid_baseline(root: Path) -> None:
    policy_path = root / "policies/cowork.json"
    suite_path = root / "suites/suite.json"
    body: dict[str, Any] = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "generated_at": "2026-08-24T00:00:00+00:00",
        "kind": "cowork",
        "dataset": "suite-v1",
        "dataset_version": "1",
        "dataset_fingerprint": hashlib.sha256(suite_path.read_bytes()).hexdigest(),
        "config_fingerprint": "b" * 64,
        "git_sha": "c" * 40,
        "git_dirty": False,
        "review": {
            "origin": "synthetic",
            "status": "approved",
            "reviewer": "fixture-owner",
            "reviewed_at": "2026-08-24T00:00:00+00:00",
        },
        "selection": {"split_counts": {"dev": 1}},
        "calibration_fingerprint": None,
        "label": "baseline-v1",
        "policy": {
            "name": "cowork-policy-v1",
            "sha256": hashlib.sha256(
                canonical_json(json.loads(policy_path.read_text(encoding="utf-8"))).encode("utf-8")
            ).hexdigest(),
        },
        "source_report_sha256": "d" * 64,
        "cases": [
            {
                "case_id": "case-1",
                "segment": "dev",
                "status": "completed",
                "error": False,
                "metrics": {"task_success": {"numerator": 1.0, "denominator": 1.0}},
            }
        ],
    }
    body["integrity"] = {
        "algorithm": "sha256",
        "value": hashlib.sha256(canonical_json(body).encode()).hexdigest(),
    }
    _write_json(root / "baselines/cowork.json", body)


def test_repository_catalog_is_healthy_but_exposes_legacy_baseline_warnings() -> None:
    report = doctor_catalog(DEFAULT_CATALOG, repo_root=REPO_ROOT)

    assert report.healthy is True
    assert report.status == "warning"
    assert [resource.health for resource in report.resources] == [
        "warning",
        "warning",
        "warning",
        "warning",
        "ready",
    ]
    rebuilds = [issue for issue in report.issues if issue.code == "baseline_rebuild_required"]
    assert {issue.resource_id for issue in rebuilds} == {
        "cowork-core-dev",
        "cowork-core-test",
        "kb-retrieval",
        "grounded-generation",
    }


def test_cli_json_distinguishes_ready_and_warning(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor", "--catalog", str(DEFAULT_CATALOG), "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["healthy"] is True
    assert output["status"] == "warning"
    assert output["summary"] == {
        "error_count": 0,
        "invalid": 0,
        "ready": 1,
        "resource_count": 5,
        "warning_count": 4,
        "warnings": 4,
    }


def test_rebuild_required_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    report = _doctor(tmp_path, _fixture_tree(tmp_path))

    assert report.healthy is True
    assert report.status == "warning"
    assert _codes(report) == {"baseline_rebuild_required"}
    assert report.resources[0].health == "warning"
    assert report.resources[1].health == "ready"


def test_ready_baseline_requires_promoted_schema_and_then_becomes_ready(tmp_path: Path) -> None:
    catalog = _fixture_tree(tmp_path, baseline_status="ready")
    legacy = _doctor(tmp_path, catalog)
    assert legacy.healthy is False
    assert "baseline_schema_invalid" in _codes(legacy)

    _promote_valid_baseline(tmp_path)
    ready = _doctor(tmp_path, catalog)
    assert ready.healthy is True
    assert ready.status == "ready"
    assert all(resource.health == "ready" for resource in ready.resources)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda catalog: catalog.update(schema_version="v0"), "catalog_schema_invalid"),
        (lambda catalog: catalog["tracks"][0].update(kind="unknown"), "track_kind_invalid"),
        (
            lambda catalog: catalog["tracks"][0]["baseline"].update(status="legacy"),
            "baseline_status_invalid",
        ),
        (
            lambda catalog: catalog["tracks"][0]["baseline"].pop("reason"),
            "baseline_rebuild_reason_missing",
        ),
        (lambda catalog: catalog.update(replay_suites=[]), "replay_suites_invalid"),
    ],
)
def test_catalog_schema_errors_return_unhealthy(
    tmp_path: Path,
    mutation,
    expected_code: str,
) -> None:
    catalog = _fixture_tree(tmp_path)
    mutation(catalog)

    report = _doctor(tmp_path, catalog)

    assert report.healthy is False
    assert expected_code in _codes(report)


def test_ids_are_unique_across_tracks_and_replays(tmp_path: Path) -> None:
    catalog = _fixture_tree(tmp_path)
    catalog["replay_suites"][0]["id"] = "cowork-core"

    report = _doctor(tmp_path, catalog)

    assert report.healthy is False
    assert "resource_id_duplicate" in _codes(report)
    assert all(resource.health == "invalid" for resource in report.resources)


@pytest.mark.parametrize("path", ["../outside.json", "/tmp/outside.json"])
def test_referenced_paths_cannot_escape_repo(tmp_path: Path, path: str) -> None:
    catalog = _fixture_tree(tmp_path)
    catalog["tracks"][0]["suite"] = path

    report = _doctor(tmp_path, catalog)

    assert report.healthy is False
    assert _codes(report) & {"path_escape", "path_absolute"}


def test_symlink_to_file_outside_repo_is_rejected(tmp_path: Path) -> None:
    catalog = _fixture_tree(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-suite.json"
    _write_json(outside, {"name": "outside"})
    (tmp_path / "suites/escape.json").symlink_to(outside)
    catalog["tracks"][0]["suite"] = "suites/escape.json"

    report = _doctor(tmp_path, catalog)

    assert report.healthy is False
    assert "path_escape" in _codes(report)


@pytest.mark.parametrize(
    "field",
    ["suite", "policy", "baseline.path", "replay.path"],
)
def test_missing_referenced_files_are_errors(tmp_path: Path, field: str) -> None:
    catalog = _fixture_tree(tmp_path)
    if field == "baseline.path":
        catalog["tracks"][0]["baseline"]["path"] = "missing.json"
    elif field == "replay.path":
        catalog["replay_suites"][0]["path"] = "missing.json"
    else:
        catalog["tracks"][0][field] = "missing.json"

    report = _doctor(tmp_path, catalog)

    assert report.healthy is False
    assert "path_missing" in _codes(report)


def test_policy_kind_must_match_track_kind(tmp_path: Path) -> None:
    catalog = _fixture_tree(tmp_path)
    policy_path = tmp_path / "policies/cowork.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["report_kind"] = "generation"
    _write_json(policy_path, policy)

    report = _doctor(tmp_path, catalog)

    assert report.healthy is False
    assert "policy_kind_mismatch" in _codes(report)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda baseline: baseline.update(kind="generation"), "baseline_kind_mismatch"),
        (lambda baseline: baseline.update(dataset="different-suite"), "baseline_suite_mismatch"),
        (
            lambda baseline: baseline["policy"].update(sha256="0" * 64),
            "baseline_policy_hash_mismatch",
        ),
        (
            lambda baseline: baseline["integrity"].update(value="0" * 64),
            "baseline_integrity_mismatch",
        ),
        (lambda baseline: baseline.update(git_dirty=True), "baseline_git_dirty"),
        (
            lambda baseline: baseline["review"].update(reviewer="somebody-else"),
            "baseline_review_mismatch",
        ),
        (
            lambda baseline: baseline["selection"]["split_counts"].update(dev=2),
            "baseline_selection_mismatch",
        ),
    ],
)
def test_ready_baseline_provenance_is_verified(
    tmp_path: Path,
    mutation,
    expected_code: str,
) -> None:
    catalog = _fixture_tree(tmp_path, baseline_status="ready")
    _promote_valid_baseline(tmp_path)
    path = tmp_path / "baselines/cowork.json"
    baseline = json.loads(path.read_text(encoding="utf-8"))
    mutation(baseline)
    _write_json(path, baseline)

    report = _doctor(tmp_path, catalog)

    assert report.healthy is False
    assert expected_code in _codes(report)


def test_ready_baseline_requires_complete_reproducibility_provenance(
    tmp_path: Path,
) -> None:
    catalog = _fixture_tree(tmp_path, baseline_status="ready")
    _promote_valid_baseline(tmp_path)
    path = tmp_path / "baselines/cowork.json"
    baseline = json.loads(path.read_text(encoding="utf-8"))
    baseline.pop("config_fingerprint")
    baseline.pop("integrity")
    baseline["integrity"] = {
        "algorithm": "sha256",
        "value": hashlib.sha256(canonical_json(baseline).encode()).hexdigest(),
    }
    _write_json(path, baseline)

    report = _doctor(tmp_path, catalog)

    assert report.healthy is False
    assert "baseline_provenance_invalid" in _codes(report)


def test_duplicate_json_keys_and_bad_replay_schema_are_rejected(tmp_path: Path) -> None:
    catalog = _fixture_tree(tmp_path)
    (tmp_path / "policies/cowork.json").write_text(
        '{"schema_version":"workpilot-regression-policy.v1","name":"a","name":"b",'
        '"report_kind":"cowork"}',
        encoding="utf-8",
    )
    replay_path = tmp_path / "replays/events.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    replay["schema_version"] = 2
    _write_json(replay_path, replay)

    report = _doctor(tmp_path, catalog)

    assert report.healthy is False
    assert {"json_invalid", "replay_schema_invalid"} <= _codes(report)


def test_invalid_catalog_cli_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "catalog.json"
    _write_json(path, {"schema_version": "unsupported", "tracks": [], "replay_suites": []})

    assert main(["doctor", "--catalog", str(path)]) == 2
    assert "INVALID" in capsys.readouterr().out


def test_mutating_one_fixture_does_not_leak_to_another(tmp_path: Path) -> None:
    """守住测试 helper 的可变结构边界，避免误报 catalog 验证结果。"""

    first = _fixture_tree(tmp_path)
    second = deepcopy(first)
    first["tracks"][0]["baseline"]["reason"] = "changed"
    assert second["tracks"][0]["baseline"]["reason"] == "legacy runner report"
