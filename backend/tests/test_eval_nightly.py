from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

import eval.nightly as nightly
from eval.nightly import (
    CommandResult,
    NightlyError,
    NightlyLimits,
    TrackSpec,
    _bounded_live_command,
    _report_model_usage,
    _run_command,
    build_track_specs,
    run_nightly,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_nightly_matrix_runs_all_promoted_tracks_with_hard_resource_fuses(
    tmp_path: Path,
) -> None:
    specs = build_track_specs(
        tmp_path / "nightly",
        authorization_note="approved scheduled synthetic evaluation",
        python="python-fixture",
    )

    assert [item.track_id for item in specs] == [
        "cowork-core-dev",
        "cowork-core-test",
        "kb-retrieval",
        "grounded-generation",
        "agent-teams-contract",
        "control-plane-contract",
    ]
    for item in (specs[0], specs[1], specs[3]):
        assert item.command.count("--max-total-tokens") == 1
        assert item.command.count("--max-model-calls") == 1
        assert item.command.count("--max-wall-seconds") == 1
    assert "--include-test" in specs[1].command
    assert "--refusal-calibration" in specs[2].command
    assert dict(specs[2].env_overrides) == {"RERANK_ENABLED": "true"}
    assert "--max-evidence-chars" in specs[3].command
    assert dict(specs[3].env_overrides) == {"RERANK_ENABLED": "false"}
    assert specs[3].allow_config_drift is True
    assert specs[4].baseline is None and specs[4].policy is None
    assert specs[4].command[:4] == ("python-fixture", "-m", "pytest", "-q")
    assert any("test_write_scope_is_bound" in value for value in specs[4].command)
    assert specs[5].command[:4] == ("python-fixture", "-m", "pytest", "-q")
    assert any("test_mcp_oauth_token_is_forwarded" in value for value in specs[5].command)
    assert all("--allow-model-send" not in value for spec in specs[4:] for value in spec.command)


def test_remaining_nightly_budget_tightens_each_live_subprocess() -> None:
    spec = build_track_specs(
        Path("/tmp/nightly-fixture"),
        authorization_note="approved",
        python="python-fixture",
    )[0]

    command = _bounded_live_command(
        spec,
        remaining_tokens=1234,
        remaining_calls=7,
        remaining_wall_seconds=8.5,
    )

    assert command[command.index("--max-total-tokens") + 1] == "1234"
    assert command[command.index("--max-model-calls") + 1] == "7"
    assert command[command.index("--max-wall-seconds") + 1] == "8.5"


def test_report_usage_is_required_and_unsettled_reservations_fail_closed(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "resource_limits": {
                    "usage": {
                        "status": "within_limits",
                        "model_calls": 3,
                        "total_tokens": 42,
                        "reserved_tokens": 0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert _report_model_usage(report) == (3, 42)

    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["resource_limits"]["usage"]["reserved_tokens"] = 1
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(NightlyError, match="unsettled"):
        _report_model_usage(report)

    del payload["resource_limits"]["usage"]["reserved_tokens"]
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(NightlyError, match="no reservation counter"):
        _report_model_usage(report)


def test_subprocess_wall_timeout_terminates_the_whole_process_group(tmp_path: Path) -> None:
    sentinel = tmp_path / "child-survived.txt"
    script = tmp_path / "parent.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', "
        + repr(
            "import time; from pathlib import Path; "
            f"time.sleep(0.5); Path({str(sentinel)!r}).write_text('survived')"
        )
        + "])\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    log = tmp_path / "track.log"

    result = _run_command(
        (sys.executable, str(script)),
        repo_root=tmp_path,
        env={},
        log_path=log,
        timeout_seconds=0.1,
    )
    time.sleep(0.6)

    assert result.returncode == 124
    assert result.timed_out is True
    assert "terminated after wall timeout" in log.read_text(encoding="utf-8")
    assert not sentinel.exists()


def test_nightly_summary_uses_the_same_hard_limits_and_metered_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Doctor:
        healthy = True
        status = "ready"

        @staticmethod
        def to_json() -> str:
            return "{}\n"

    spec = TrackSpec(
        "live-fixture",
        "baseline.json",
        "policy.json",
        (
            "python-fixture",
            "live-fixture",
            "--max-total-tokens",
            "80",
            "--max-model-calls",
            "8",
            "--max-wall-seconds",
            "50",
        ),
        "raw/live/report.json",
        max_total_tokens=80,
        max_model_calls=8,
        max_wall_seconds=50,
    )

    monkeypatch.setattr(nightly, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(nightly, "_git_state", lambda _root: ("a" * 40, False))
    monkeypatch.setattr(nightly, "doctor_catalog", lambda *_args, **_kwargs: _Doctor())
    monkeypatch.setattr(
        nightly,
        "verify_full_chain_cassette",
        lambda _path: {"valid": True, "real_io_calls": 0},
    )
    monkeypatch.setattr(nightly, "build_track_specs", lambda *_args, **_kwargs: (spec,))

    observed_envs: list[dict[str, str]] = []

    def fake_run(command, *, repo_root, env, log_path, timeout_seconds):
        del repo_root, timeout_seconds
        observed_envs.append(dict(env))
        if "live-fixture" in command:
            report = log_path.parents[1] / "live" / "report.json"
            report.parent.mkdir(parents=True)
            report.write_text(
                json.dumps(
                    {
                        "resource_limits": {
                            "usage": {
                                "status": "within_limits",
                                "model_calls": 2,
                                "total_tokens": 7,
                                "reserved_tokens": 0,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
        return CommandResult(returncode=0, timed_out=False, wall_seconds=0.1)

    monkeypatch.setattr(nightly, "_run_command", fake_run)
    configured = NightlyLimits(
        max_total_tokens=100,
        max_model_calls=10,
        max_wall_seconds=60,
    )

    _package, summary = run_nightly(
        output_root=tmp_path / "out",
        authorization_note="approved",
        limits=configured,
    )

    assert summary["passed"] is True
    assert summary["schema_version"] == "workpilot-eval-nightly-summary.v2"
    assert summary["resource_limits"]["limits"] == {
        "max_total_tokens": 100,
        "max_model_calls": 10,
        "max_wall_seconds": 60,
    }
    assert summary["resource_limits"]["usage"]["model_calls"] == 2
    assert summary["resource_limits"]["usage"]["total_tokens"] == 7
    assert all("RERANK_ENABLED" not in value for value in observed_envs)


def test_nightly_raw_and_upload_paths_are_separated(tmp_path: Path) -> None:
    package = tmp_path / "nightly"
    specs = build_track_specs(package, authorization_note="approved", python="python-fixture")

    reports = [item.report_glob for item in specs if item.report_glob is not None]
    assert all(item.startswith("raw/") for item in reports)
    assert all("artifacts" not in item for item in reports)


def test_scheduled_workflow_uses_private_runner_and_bounded_retention() -> None:
    workflow = (REPO_ROOT / ".github/workflows/eval-nightly.yml").read_text()

    assert 'cron: "0 18 * * *"' in workflow
    assert "runs-on: [self-hosted, workpilot-eval]" in workflow
    assert "python -m eval.nightly" in workflow
    assert "--max-total-tokens 6000000" in workflow
    assert "--max-model-calls 1200" in workflow
    assert "--max-wall-seconds 18000" in workflow
    assert "python -m eval.full_chain_cassette" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "path: ${{ steps.artifact.outputs.path }}" in workflow
    assert "retention-days: 30" in workflow
    assert "python -m eval.artifact_retention" in workflow
    assert "--older-than-days 30 --keep-latest 7 --apply" in workflow
