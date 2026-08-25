from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from eval.generation_report_merge import (
    GenerationRetryMergeError,
    merge_infrastructure_retries,
)


def _report(*, error: str | None, selection: object = "all") -> dict:
    return {
        "kind": "generation",
        "dataset": "suite-v2",
        "git_sha": "clean-sha",
        "suite": {"sha256": "suite-sha"},
        "config": {
            "selection": selection,
            "dataset": "suite-v2",
            "chat_model": "deepseek-v4-flash",
            "chat_provider": "openai_compatible",
            "kb_index_fingerprint": "index",
            "routing_fingerprint": "route",
        },
        "kb": {"slug": "frozen", "version_id": "v1"},
        "reproducibility": {
            "git_dirty": False,
            "implementation_fingerprint": "impl",
        },
        "items": [
            {
                "item_id": "case-1",
                "category": "multi_hop",
                "answerable": True,
                "error": error,
                "model": None if error else "deepseek-v4-flash",
                "provider": None if error else "openai_compatible",
                "refused": False,
                "refusal_correct": error is None,
                "citation_validity": {"valid": error is None},
                "citation_gold_alignment": {"aligned": 0, "total": 1},
                "constraint_pass": {"passed": False},
                "latency_ms": 10,
                "total_tokens": 100,
                "attempts": 2,
            }
        ],
    }


def test_merge_replaces_only_error_and_reaggregates(tmp_path: Path) -> None:
    source_path = tmp_path / "source.json"
    retry_path = tmp_path / "retry.json"
    source_path.write_text("{}")
    retry_path.write_text("{}")
    merged = merge_infrastructure_retries(
        _report(error="ProviderRouteTimeoutError: timeout"),
        [_report(error=None, selection=["case-1"])],
        source_path=source_path,
        retry_paths=[retry_path],
        label="merged",
    )

    assert merged["items"][0]["error"] is None
    assert merged["metrics"]["error_count"] == 0
    assert merged["metrics"]["refusal_correct"]["value"] == 1.0
    assert merged["infrastructure_retry_merge"]["replacements"][0]["item_id"] == "case-1"


def test_merge_rejects_quality_cherry_pick(tmp_path: Path) -> None:
    with pytest.raises(GenerationRetryMergeError, match="禁止挑选替换"):
        merge_infrastructure_retries(
            _report(error=None),
            [_report(error=None, selection=["case-1"])],
            source_path=tmp_path / "source.json",
            retry_paths=[tmp_path / "retry.json"],
            label="merged",
        )


def test_merge_rejects_contract_drift(tmp_path: Path) -> None:
    retry = deepcopy(_report(error=None, selection=["case-1"]))
    retry["config"]["chat_model"] = "other"
    with pytest.raises(GenerationRetryMergeError, match="合同漂移"):
        merge_infrastructure_retries(
            _report(error="ProviderRouteTimeoutError: timeout"),
            [retry],
            source_path=tmp_path / "source.json",
            retry_paths=[tmp_path / "retry.json"],
            label="merged",
        )
