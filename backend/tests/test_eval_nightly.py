from __future__ import annotations

from pathlib import Path

from eval.nightly import build_track_specs

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_nightly_matrix_runs_all_promoted_tracks_without_token_fuse(tmp_path: Path) -> None:
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
    ]
    assert all("--budget-tokens" not in item.command for item in specs)
    assert "--include-test" in specs[1].command
    assert "--refusal-calibration" in specs[2].command
    assert "--max-evidence-chars" in specs[3].command
    assert specs[3].allow_config_drift is True


def test_nightly_raw_and_upload_paths_are_separated(tmp_path: Path) -> None:
    package = tmp_path / "nightly"
    specs = build_track_specs(package, authorization_note="approved", python="python-fixture")

    assert all(item.report_glob.startswith("raw/") for item in specs)
    assert all("artifacts" not in item.report_glob for item in specs)


def test_scheduled_workflow_uses_private_runner_and_bounded_retention() -> None:
    workflow = (REPO_ROOT / ".github/workflows/eval-nightly.yml").read_text()

    assert 'cron: "0 18 * * *"' in workflow
    assert "runs-on: [self-hosted, workpilot-eval]" in workflow
    assert "python -m eval.nightly" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "path: ${{ steps.artifact.outputs.path }}" in workflow
    assert "retention-days: 30" in workflow
    assert "python -m eval.artifact_retention" in workflow
    assert "--older-than-days 30 --keep-latest 7 --apply" in workflow
