from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from eval.cowork_report_merge import CoworkRetryMergeError, merge_infrastructure_retries


def _report(item_id: str, status: str) -> dict:
    return {
        "kind": "cowork",
        "suite": "suite",
        "suite_version": "1",
        "config": {},
        "config_hash": "config",
        "reproducibility": {"implementation_fingerprint": "impl"},
        "manifest": {
            "suite_sha256": "suite-sha",
            "model": {"model": "fixed"},
            "budgets": {"tokens": 0, "calls": 0, "wall_ms": 0},
            "fixture_policy": {"network": "fixture"},
            "config": {
                "runtime": {"fallback_enabled": False},
            },
            "reproducibility": {"implementation_fingerprint": "impl"},
        },
        "items": [
            {
                "item_id": item_id,
                "observation": {"status": status, "error": None},
                "score": {"task_success": status == "done"},
            }
        ],
    }


def test_merge_replaces_only_infrastructure_failure(tmp_path: Path) -> None:
    source_path = tmp_path / "source.json"
    retry_path = tmp_path / "retry.json"
    source_path.write_text("{}")
    retry_path.write_text("{}")

    merged = merge_infrastructure_retries(
        _report("case-1", "failed"),
        [_report("case-1", "done")],
        source_path=source_path,
        retry_paths=[retry_path],
        label="merged",
    )

    assert merged["items"][0]["observation"]["status"] == "done"
    audit = merged["manifest"]["retry_replacements"]
    assert audit[0]["old_status"] == "failed"
    assert audit[0]["new_status"] == "done"


def test_merge_rejects_quality_result_replacement(tmp_path: Path) -> None:
    source = _report("case-1", "done")
    source["items"][0]["score"]["task_success"] = False
    with pytest.raises(CoworkRetryMergeError, match="禁止挑选替换"):
        merge_infrastructure_retries(
            source,
            [_report("case-1", "done")],
            source_path=tmp_path / "source.json",
            retry_paths=[tmp_path / "retry.json"],
            label="merged",
        )


def test_merge_rejects_contract_drift(tmp_path: Path) -> None:
    retry = deepcopy(_report("case-1", "done"))
    retry["manifest"]["model"] = {"model": "other"}
    with pytest.raises(CoworkRetryMergeError, match="合同漂移"):
        merge_infrastructure_retries(
            _report("case-1", "failed"),
            [retry],
            source_path=tmp_path / "source.json",
            retry_paths=[tmp_path / "retry.json"],
            label="merged",
        )
