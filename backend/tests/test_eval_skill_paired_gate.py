from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from eval.skill_paired_gate import evaluate, main

SUITE = Path(__file__).resolve().parents[2] / "eval/suites/skill-paired-dev-v1.json"


def _report(mode: str, *, anti_activation: bool = False) -> dict[str, object]:
    suite = json.loads(SUITE.read_text(encoding="utf-8"))
    trigger = set(suite["skill_pairing"]["trigger_item_ids"])
    items = []
    for item in suite["items"]:
        item_id = item["id"]
        enabled = mode == "enabled"
        loaded = enabled and (item_id in trigger or anti_activation)
        success = item_id not in trigger or enabled
        trace = []
        if loaded:
            trace.append(
                {
                    "name": "load_skill",
                    "status": "ok",
                    "arguments": {"name": "incident-handoff"},
                }
            )
        trace.append({"name": "read_file", "status": "ok", "arguments": {"path": "x"}})
        items.append(
            {
                "item_id": item_id,
                "observation": {
                    "used_tokens": 120 if enabled else 100,
                    "used_calls": len(trace),
                    "tool_trace": trace,
                },
                "score": {"task_success": success, "guardrail_pass": True},
            }
        )
    return {
        "schema_version": "cowork-eval-report.v1",
        "suite": suite["name"],
        "manifest": {
            "suite_sha256": hashlib.sha256(SUITE.read_bytes()).hexdigest(),
            "reproducibility": {"git_dirty": False},
            "config": {
                "model": {"provider": "fixture", "model": "fixed"},
                "runtime": {
                    "skills_mode": mode,
                    "skills_root_sha256": "a" * 64,
                },
            },
        },
        "items": items,
    }


def _write(tmp_path: Path, name: str, value: dict[str, object]) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_skill_paired_gate_passes_useful_non_triggering_candidate(tmp_path: Path) -> None:
    report = evaluate(
        suite_path=SUITE,
        off_report_path=_write(tmp_path, "off", _report("disabled")),
        on_report_path=_write(tmp_path, "on", _report("enabled")),
    )

    assert report["passed"] is True
    assert report["claim_scope"] == "engineering_only_no_product_claim"
    assert report["metrics"]["trigger_activation_rate"] == 1.0
    assert report["metrics"]["anti_trigger_activation_rate"] == 0.0


def test_skill_paired_gate_blocks_anti_trigger_activation(tmp_path: Path) -> None:
    report = evaluate(
        suite_path=SUITE,
        off_report_path=_write(tmp_path, "off", _report("disabled")),
        on_report_path=_write(tmp_path, "on", _report("enabled", anti_activation=True)),
    )

    assert report["passed"] is False
    assert any(item["rule"] == "anti_trigger_activation" for item in report["violations"])


def test_skill_paired_gate_writes_three_state_refusal_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "refused"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "skill-paired-gate",
            "--suite",
            str(SUITE),
            "--disabled-report",
            str(tmp_path / "missing.json"),
            "--enabled-report",
            str(_write(tmp_path, "on", _report("enabled"))),
            "--output-dir",
            str(output),
        ],
    )

    with pytest.raises(SystemExit) as stopped:
        main()

    assert stopped.value.code == 2
    refusal = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert refusal["status"] == "refused"
    assert refusal["passed"] is False
    assert "missing.json" not in json.dumps(refusal)
