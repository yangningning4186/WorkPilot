"""Run every promoted evaluation track and compare it with the v2 baselines.

Raw reports and model cassettes stay under the ignored local package.  The
``artifacts`` directory contains only privacy-safe doctor/regression output and
is the sole directory intended for CI artifact upload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from eval.catalog import DEFAULT_CATALOG, doctor_catalog
from eval.full_chain_cassette import verify as verify_full_chain_cassette


class NightlyError(RuntimeError):
    """The nightly run could not satisfy its reproducibility contract."""


@dataclass(frozen=True)
class TrackSpec:
    track_id: str
    baseline: str
    policy: str
    command: tuple[str, ...]
    report_glob: str
    allow_config_drift: bool = False


@dataclass(frozen=True)
class TrackResult:
    track_id: str
    status: str
    report_sha256: str | None
    regression_status: str | None
    error: str | None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_state(repo_root: Path) -> tuple[str, bool]:
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_root, text=True).strip()
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo_root,
        text=True,
    )
    return sha, bool(status.strip())


def build_track_specs(
    package: Path,
    *,
    authorization_note: str,
    python: str = sys.executable,
) -> tuple[TrackSpec, ...]:
    """Build the frozen nightly matrix; paths are intentionally explicit."""

    raw = package / "raw"
    shared = (
        "--allow-synthetic",
        "--allow-model-send",
        "--authorization-note",
        authorization_note,
    )
    return (
        TrackSpec(
            "cowork-core-dev",
            "eval/snapshots/v2/cowork-core-dev.json",
            "eval/policies/cowork.json",
            (
                python,
                "-m",
                "eval.cowork_runner",
                "--suite",
                "eval/suites/cowork-core-50-v1.6.1.json",
                "--label",
                "nightly-cowork-dev",
                "--split",
                "dev",
                *shared,
                "--output-root",
                str(raw / "cowork-dev"),
            ),
            "raw/cowork-dev/*/report.json",
        ),
        TrackSpec(
            "cowork-core-test",
            "eval/snapshots/v2/cowork-core-test.json",
            "eval/policies/cowork.json",
            (
                python,
                "-m",
                "eval.cowork_runner",
                "--suite",
                "eval/suites/cowork-core-50-v1.6.1.json",
                "--label",
                "nightly-cowork-test",
                "--split",
                "test",
                "--include-test",
                "--test-access-note",
                "scheduled frozen-test regression; no tuning",
                *shared,
                "--output-root",
                str(raw / "cowork-test"),
            ),
            "raw/cowork-test/*/report.json",
        ),
        TrackSpec(
            "kb-retrieval",
            "eval/snapshots/v2/kb-retrieval.json",
            "eval/policies/retrieval.json",
            (
                python,
                "-m",
                "eval.kb_retrieval_runner",
                "run",
                "--suite",
                "eval/suites/kb-rag-research-dev-v1.json",
                "--kb-slug",
                "rag-research",
                "--kb-version",
                "v1",
                "--label",
                "nightly-kb-retrieval",
                "--top-k",
                "10",
                "--diagnostic-k",
                "50",
                "--token-budget",
                "4000",
                "--refusal-calibration",
                "eval/calibrations/kb-rag-research-refusal-v1.json",
                "--allow-synthetic",
                "--output-dir",
                str(raw / "kb-retrieval"),
            ),
            "raw/kb-retrieval/report.json",
        ),
        TrackSpec(
            "grounded-generation",
            "eval/snapshots/v2/generation.json",
            "eval/policies/generation.json",
            (
                python,
                "-m",
                "eval.generation_runner",
                "run",
                "--suite",
                "eval/suites/m1-dev-70-v2.json",
                "--label",
                "nightly-generation-v2",
                "--allow-model-send",
                "--authorization-note",
                authorization_note,
                "--kb-version",
                "v1",
                "--top-k",
                "10",
                "--max-evidence-chars",
                "12000",
                "--concurrency",
                "3",
                "--retry-attempts",
                "2",
                "--output-root",
                str(raw / "generation"),
            ),
            "raw/generation/*/report.json",
            # Generation config records runner_git_sha for forensic audit.  A
            # nightly candidate necessarily runs after the baseline commit;
            # dataset/model/KB/index drift is still rejected by the runner and
            # paired cases/metrics remain strictly checked here.
            allow_config_drift=True,
        ),
    )


def _write_private(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _run_command(
    command: Sequence[str],
    *,
    repo_root: Path,
    env: dict[str, str],
    log_path: Path,
) -> int:
    completed = subprocess.run(
        command,
        cwd=repo_root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    _write_private(log_path, completed.stdout)
    return completed.returncode


def _single_report(package: Path, pattern: str) -> Path:
    matches = sorted(package.glob(pattern))
    if len(matches) != 1:
        raise NightlyError(f"expected one report for {pattern}, found {len(matches)}")
    return matches[0]


def run_nightly(
    *,
    output_root: Path,
    authorization_note: str,
) -> tuple[Path, dict[str, object]]:
    repo_root = _repo_root()
    git_sha, git_dirty = _git_state(repo_root)
    if git_dirty:
        raise NightlyError("nightly requires a clean Git worktree")
    if not authorization_note.strip():
        raise NightlyError("model-send authorization note is required")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    package = output_root / f"{stamp}-full-v2"
    package.mkdir(parents=True, exist_ok=False, mode=0o700)
    (package / "raw/logs").mkdir(parents=True, mode=0o700)
    artifacts = package / "artifacts"
    (artifacts / "regression").mkdir(parents=True)

    doctor = doctor_catalog(DEFAULT_CATALOG, repo_root=repo_root)
    (artifacts / "catalog-doctor.json").write_text(doctor.to_json(), encoding="utf-8")
    if not doctor.healthy or doctor.status != "ready":
        raise NightlyError(f"catalog doctor must be ready, got {doctor.status}")
    cassette_report = verify_full_chain_cassette(repo_root / "eval/replays/full-chain-v1.json")
    (artifacts / "full-chain-cassette.json").write_text(
        json.dumps(cassette_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    backend = str(repo_root / "backend")
    env["PYTHONPATH"] = (
        backend if not env.get("PYTHONPATH") else f"{backend}{os.pathsep}{env['PYTHONPATH']}"
    )
    # Formal baselines use the fusion score source.  A configured reranker may
    # never silently fall back inside the retrieval/generation tracks.
    env["RERANK_ENABLED"] = "false"

    results: list[TrackResult] = []
    for spec in build_track_specs(package, authorization_note=authorization_note):
        log_path = package / "raw/logs" / f"{spec.track_id}.log"
        code = _run_command(spec.command, repo_root=repo_root, env=env, log_path=log_path)
        if code != 0:
            results.append(
                TrackResult(spec.track_id, "runner_failed", None, None, f"exit_code={code}")
            )
            continue
        try:
            report = _single_report(package, spec.report_glob)
        except NightlyError as error:
            results.append(TrackResult(spec.track_id, "runner_failed", None, None, str(error)))
            continue

        regression_dir = artifacts / "regression" / spec.track_id
        command = [
            sys.executable,
            "-m",
            "eval.regression",
            "check",
            str(report),
            "--baseline",
            spec.baseline,
            "--policy",
            spec.policy,
            "--output-dir",
            str(regression_dir),
        ]
        if spec.allow_config_drift:
            command.append("--allow-config-drift")
        regression_log = package / "raw/logs" / f"{spec.track_id}-regression.log"
        regression_code = _run_command(
            command, repo_root=repo_root, env=env, log_path=regression_log
        )
        results.append(
            TrackResult(
                spec.track_id,
                "passed" if regression_code == 0 else "regression_failed",
                _sha256(report),
                "passed" if regression_code == 0 else f"exit_code={regression_code}",
                None,
            )
        )

    passed = all(item.status == "passed" for item in results) and len(results) == 4
    summary: dict[str, object] = {
        "schema_version": "workpilot-eval-nightly-summary.v1",
        "run_id": package.name,
        "generated_at": datetime.now(UTC).isoformat(),
        "git_sha": git_sha,
        "git_dirty": False,
        "catalog_status": doctor.status,
        "full_chain_cassette": {
            "valid": cassette_report["valid"],
            "real_io_calls": cassette_report["real_io_calls"],
        },
        "model_token_fuse": "disabled",
        "raw_retention": {"days": 30, "keep_latest": 7, "uploaded": False},
        "artifact_retention_days": 30,
        "passed": passed,
        "tracks": [asdict(item) for item in results],
    }
    (artifacts / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return package, summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=Path("eval/outputs/nightly"))
    parser.add_argument("--authorization-note", required=True)
    args = parser.parse_args(argv)
    try:
        package, summary = run_nightly(
            output_root=args.output_root,
            authorization_note=args.authorization_note,
        )
    except NightlyError as error:
        print(f"nightly refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"package": str(package), "passed": summary["passed"]}, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
