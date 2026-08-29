"""Run every promoted track plus zero-model contracts and enforce their gates.

Raw reports and model cassettes stay under the ignored local package.  The
``artifacts`` directory contains only privacy-safe doctor/regression output and
is the sole directory intended for CI artifact upload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from eval.catalog import DEFAULT_CATALOG, doctor_catalog
from eval.cowork_task_suite import deterministic_contract, load_suite
from eval.deterministic_contract import load_contract
from eval.full_chain_cassette import verify as verify_full_chain_cassette


class NightlyError(RuntimeError):
    """The nightly run could not satisfy its reproducibility contract."""


DEFAULT_MAX_TOTAL_TOKENS = 6_000_000
DEFAULT_MAX_MODEL_CALLS = 1_200
DEFAULT_MAX_WALL_SECONDS = 18_000.0
DEFAULT_REGRESSION_WALL_SECONDS = 600.0


@dataclass(frozen=True)
class NightlyLimits:
    max_total_tokens: int = DEFAULT_MAX_TOTAL_TOKENS
    max_model_calls: int = DEFAULT_MAX_MODEL_CALLS
    max_wall_seconds: float = DEFAULT_MAX_WALL_SECONDS

    def __post_init__(self) -> None:
        if self.max_total_tokens < 1:
            raise ValueError("max_total_tokens must be positive")
        if self.max_model_calls < 1:
            raise ValueError("max_model_calls must be positive")
        if self.max_wall_seconds <= 0:
            raise ValueError("max_wall_seconds must be positive")


@dataclass(frozen=True)
class TrackSpec:
    track_id: str
    baseline: str | None
    policy: str | None
    command: tuple[str, ...]
    report_glob: str | None
    allow_config_drift: bool = False
    max_total_tokens: int | None = None
    max_model_calls: int | None = None
    max_wall_seconds: float = 600.0
    env_overrides: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TrackResult:
    track_id: str
    status: str
    report_sha256: str | None
    regression_status: str | None
    error: str | None
    model_calls: int = 0
    total_tokens: int = 0
    wall_seconds: float = 0.0


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    timed_out: bool
    wall_seconds: float


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
        "--max-total-tokens",
        "2000000",
        "--max-model-calls",
        "400",
        "--max-wall-seconds",
        "5000",
    )
    repo_root = _repo_root()
    catalog = json.loads(DEFAULT_CATALOG.read_text(encoding="utf-8"))
    contract_specs: list[TrackSpec] = []
    for raw_contract in catalog.get("contract_suites", []):
        suite_path = repo_root / str(raw_contract["suite"])
        if raw_contract.get("kind") == "cowork_deterministic":
            embedded = deterministic_contract(load_suite(suite_path))
            if embedded is None:
                raise NightlyError(f"{raw_contract['id']} 缺少 deterministic contract")
            contract = embedded
        else:
            contract = load_contract(suite_path, repo_root=repo_root)
        if contract["mode"] != raw_contract.get("mode") or contract[
            "case_count"
        ] != raw_contract.get("case_count"):
            raise NightlyError(f"{raw_contract['id']} 与 catalog 声明不一致")
        contract_specs.append(
            TrackSpec(
                str(raw_contract["id"]),
                None,
                None,
                (python, "-m", "pytest", "-q", *(str(value) for value in contract["targets"])),
                None,
                max_wall_seconds=600.0,
            )
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
            max_total_tokens=2_000_000,
            max_model_calls=400,
            max_wall_seconds=5_000.0,
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
            max_total_tokens=2_000_000,
            max_model_calls=400,
            max_wall_seconds=5_000.0,
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
            max_wall_seconds=1_800.0,
            env_overrides=(("RERANK_ENABLED", "true"),),
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
                "--max-total-tokens",
                "1500000",
                "--max-model-calls",
                "150",
                "--max-wall-seconds",
                "5000",
                "--output-root",
                str(raw / "generation"),
            ),
            "raw/generation/*/report.json",
            # Generation config records runner_git_sha for forensic audit.  A
            # nightly candidate necessarily runs after the baseline commit;
            # dataset/model/KB/index drift is still rejected by the runner and
            # paired cases/metrics remain strictly checked here.
            allow_config_drift=True,
            max_total_tokens=1_500_000,
            max_model_calls=150,
            max_wall_seconds=5_000.0,
            env_overrides=(("RERANK_ENABLED", "false"),),
        ),
        *contract_specs,
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
    timeout_seconds: float,
) -> CommandResult:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        _write_private(log_path, f"[workpilot-eval] failed to start: {type(error).__name__}\n")
        return CommandResult(
            returncode=127,
            timed_out=False,
            wall_seconds=round(max(0.0, time.monotonic() - started), 3),
        )

    def terminate_group(sig: signal.Signals) -> None:
        try:
            os.killpg(process.pid, sig)
        except OSError:
            try:
                process.send_signal(sig)
            except OSError:
                pass

    timed_out = False
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as timeout_error:
        timed_out = True
        terminate_group(signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=5.0)
        except subprocess.TimeoutExpired as kill_error:
            terminate_group(signal.SIGKILL)
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
            if process.stdout is not None:
                process.stdout.close()
            partial = kill_error.output or timeout_error.output or ""
            output = partial.decode(errors="replace") if isinstance(partial, bytes) else partial
        output = (
            output or ""
        ) + f"\n[workpilot-eval] terminated after wall timeout ({timeout_seconds:.3f}s)\n"
    wall_seconds = round(max(0.0, time.monotonic() - started), 3)
    _write_private(log_path, output or "")
    return CommandResult(
        returncode=(124 if timed_out else int(process.returncode or 0)),
        timed_out=timed_out,
        wall_seconds=wall_seconds,
    )


def _replace_flag(command: Sequence[str], flag: str, value: float) -> tuple[str, ...]:
    values = list(command)
    try:
        index = values.index(flag)
    except ValueError as error:
        raise NightlyError(f"live track command missing required fuse flag {flag}") from error
    if index + 1 >= len(values):
        raise NightlyError(f"live track command has no value for fuse flag {flag}")
    values[index + 1] = str(value)
    return tuple(values)


def _bounded_live_command(
    spec: TrackSpec,
    *,
    remaining_tokens: int,
    remaining_calls: int,
    remaining_wall_seconds: float,
) -> tuple[str, ...]:
    if spec.max_total_tokens is None and spec.max_model_calls is None:
        return spec.command
    if spec.max_total_tokens is None or spec.max_model_calls is None:
        raise NightlyError(f"{spec.track_id}: live track model fuses are incomplete")
    command = _replace_flag(
        spec.command,
        "--max-total-tokens",
        min(spec.max_total_tokens, remaining_tokens),
    )
    command = _replace_flag(
        command,
        "--max-model-calls",
        min(spec.max_model_calls, remaining_calls),
    )
    return _replace_flag(
        command,
        "--max-wall-seconds",
        min(spec.max_wall_seconds, remaining_wall_seconds),
    )


def _report_model_usage(path: Path) -> tuple[int, int]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        resource = report["resource_limits"]
        usage = resource["usage"]
        status = usage["status"]
        calls = usage["model_calls"]
        tokens = usage["total_tokens"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise NightlyError(f"{path}: model usage ledger missing or unreadable") from error
    if status != "within_limits":
        raise NightlyError(f"{path}: model usage ledger is not terminal/within_limits")
    if (
        isinstance(calls, bool)
        or not isinstance(calls, int)
        or calls < 0
        or isinstance(tokens, bool)
        or not isinstance(tokens, int)
        or tokens < 0
    ):
        raise NightlyError(f"{path}: model usage ledger has invalid counters")
    try:
        reserved = usage["reserved_tokens"]
    except (KeyError, TypeError) as error:
        raise NightlyError(f"{path}: model usage ledger has no reservation counter") from error
    if isinstance(reserved, bool) or not isinstance(reserved, int) or reserved != 0:
        raise NightlyError(f"{path}: model usage ledger has unsettled token reservations")
    return calls, tokens


def _single_report(package: Path, pattern: str) -> Path:
    matches = sorted(package.glob(pattern))
    if len(matches) != 1:
        raise NightlyError(f"expected one report for {pattern}, found {len(matches)}")
    return matches[0]


def run_nightly(
    *,
    output_root: Path,
    authorization_note: str,
    limits: NightlyLimits | None = None,
) -> tuple[Path, dict[str, object]]:
    limits = limits or NightlyLimits()
    nightly_started = time.monotonic()
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
    results: list[TrackResult] = []
    specs = build_track_specs(package, authorization_note=authorization_note)
    total_model_calls = 0
    total_tokens = 0
    fuse_status = "within_limits"
    for spec_index, spec in enumerate(specs):
        elapsed = time.monotonic() - nightly_started
        remaining_wall = limits.max_wall_seconds - elapsed
        remaining_tokens = limits.max_total_tokens - total_tokens
        remaining_calls = limits.max_model_calls - total_model_calls
        if remaining_wall <= 0 or remaining_tokens <= 0 or remaining_calls <= 0:
            fuse_status = "exhausted"
            dimension = (
                "wall_seconds"
                if remaining_wall <= 0
                else "total_tokens"
                if remaining_tokens <= 0
                else "model_calls"
            )
            results.extend(
                TrackResult(
                    pending.track_id,
                    "skipped_after_fuse",
                    None,
                    None,
                    f"nightly_{dimension}_fuse_exhausted",
                )
                for pending in specs[spec_index:]
            )
            break
        log_path = package / "raw/logs" / f"{spec.track_id}.log"
        live_command = _bounded_live_command(
            spec,
            remaining_tokens=remaining_tokens,
            remaining_calls=remaining_calls,
            remaining_wall_seconds=remaining_wall,
        )
        track_env = {**env, **dict(spec.env_overrides)}
        command_result = _run_command(
            live_command,
            repo_root=repo_root,
            env=track_env,
            log_path=log_path,
            timeout_seconds=min(spec.max_wall_seconds, remaining_wall),
        )
        if command_result.returncode != 0:
            status = "runner_timeout" if command_result.timed_out else "runner_failed"
            if command_result.timed_out:
                fuse_status = "track_timeout"
            results.append(
                TrackResult(
                    spec.track_id,
                    status,
                    None,
                    None,
                    (
                        f"wall_timeout_seconds={min(spec.max_wall_seconds, remaining_wall):.3f}"
                        if command_result.timed_out
                        else f"exit_code={command_result.returncode}"
                    ),
                    wall_seconds=command_result.wall_seconds,
                )
            )
            if spec.max_model_calls is not None:
                # A failed/terminated live subprocess may have dispatched a
                # model call without producing a complete usage ledger.  Do not
                # launch another live track against an unknowable total.
                fuse_status = "usage_unknown"
                results.extend(
                    TrackResult(
                        pending.track_id,
                        "skipped_after_fuse",
                        None,
                        None,
                        "prior_live_track_usage_unknown",
                    )
                    for pending in specs[spec_index + 1 :]
                )
                break
            continue
        if spec.report_glob is None:
            results.append(
                TrackResult(
                    spec.track_id,
                    "passed",
                    None,
                    "contract_exit_code=0",
                    None,
                    wall_seconds=command_result.wall_seconds,
                )
            )
            continue
        try:
            report = _single_report(package, spec.report_glob)
        except NightlyError as error:
            results.append(
                TrackResult(
                    spec.track_id,
                    "runner_failed",
                    None,
                    None,
                    str(error),
                    wall_seconds=command_result.wall_seconds,
                )
            )
            if spec.max_model_calls is not None:
                fuse_status = "usage_unknown"
                results.extend(
                    TrackResult(
                        pending.track_id,
                        "skipped_after_fuse",
                        None,
                        None,
                        "prior_live_track_usage_unknown",
                    )
                    for pending in specs[spec_index + 1 :]
                )
                break
            continue

        track_calls = 0
        track_tokens = 0
        if spec.max_model_calls is not None:
            try:
                track_calls, track_tokens = _report_model_usage(report)
            except NightlyError as error:
                fuse_status = "usage_unknown"
                results.append(
                    TrackResult(
                        spec.track_id,
                        "resource_usage_invalid",
                        _sha256(report),
                        None,
                        str(error),
                        wall_seconds=command_result.wall_seconds,
                    )
                )
                results.extend(
                    TrackResult(
                        pending.track_id,
                        "skipped_after_fuse",
                        None,
                        None,
                        "prior_live_track_usage_unknown",
                    )
                    for pending in specs[spec_index + 1 :]
                )
                break
            total_model_calls += track_calls
            total_tokens += track_tokens
            if total_model_calls > limits.max_model_calls or total_tokens > limits.max_total_tokens:
                fuse_status = "exceeded"
                results.append(
                    TrackResult(
                        spec.track_id,
                        "resource_limit_exceeded",
                        _sha256(report),
                        None,
                        "runner usage exceeded nightly aggregate fuse",
                        track_calls,
                        track_tokens,
                        command_result.wall_seconds,
                    )
                )
                results.extend(
                    TrackResult(
                        pending.track_id,
                        "skipped_after_fuse",
                        None,
                        None,
                        "nightly aggregate fuse exceeded",
                    )
                    for pending in specs[spec_index + 1 :]
                )
                break

        if spec.baseline is None or spec.policy is None:  # pragma: no cover - dataclass contract
            raise NightlyError(f"{spec.track_id}: regression track 缺 baseline/policy")
        regression_dir = artifacts / "regression" / spec.track_id
        regression_command = [
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
            regression_command.append("--allow-config-drift")
        regression_log = package / "raw/logs" / f"{spec.track_id}-regression.log"
        regression_remaining = limits.max_wall_seconds - (time.monotonic() - nightly_started)
        if regression_remaining <= 0:
            fuse_status = "exhausted"
            results.append(
                TrackResult(
                    spec.track_id,
                    "regression_timeout",
                    _sha256(report),
                    None,
                    "nightly wall fuse exhausted before regression",
                    track_calls,
                    track_tokens,
                    command_result.wall_seconds,
                )
            )
            continue
        regression_result = _run_command(
            regression_command,
            repo_root=repo_root,
            env=track_env,
            log_path=regression_log,
            timeout_seconds=min(DEFAULT_REGRESSION_WALL_SECONDS, regression_remaining),
        )
        if regression_result.timed_out:
            fuse_status = "track_timeout"
        results.append(
            TrackResult(
                spec.track_id,
                (
                    "passed"
                    if regression_result.returncode == 0
                    else "regression_timeout"
                    if regression_result.timed_out
                    else "regression_failed"
                ),
                _sha256(report),
                (
                    "passed"
                    if regression_result.returncode == 0
                    else "wall_timeout"
                    if regression_result.timed_out
                    else f"exit_code={regression_result.returncode}"
                ),
                None,
                track_calls,
                track_tokens,
                round(command_result.wall_seconds + regression_result.wall_seconds, 3),
            )
        )

    passed = all(item.status == "passed" for item in results) and len(results) == len(specs)
    summary: dict[str, object] = {
        "schema_version": "workpilot-eval-nightly-summary.v2",
        "run_id": package.name,
        "generated_at": datetime.now(UTC).isoformat(),
        "git_sha": git_sha,
        "git_dirty": False,
        "catalog_status": doctor.status,
        "full_chain_cassette": {
            "valid": cassette_report["valid"],
            "real_io_calls": cassette_report["real_io_calls"],
        },
        "resource_limits": {
            "limits": asdict(limits),
            "metering_scope": (
                "ModelGateway calls; local KB query embeddings are bounded by frozen "
                "item count and track wall timeout"
            ),
            "usage": {
                "model_calls": total_model_calls,
                "total_tokens": total_tokens,
                "wall_seconds": round(time.monotonic() - nightly_started, 3),
                "status": fuse_status,
            },
            "cost_usd": None,
            "cost_limit": "not_enforced_without_reliable_pricing",
        },
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
    parser.add_argument("--max-total-tokens", type=int, default=DEFAULT_MAX_TOTAL_TOKENS)
    parser.add_argument("--max-model-calls", type=int, default=DEFAULT_MAX_MODEL_CALLS)
    parser.add_argument("--max-wall-seconds", type=float, default=DEFAULT_MAX_WALL_SECONDS)
    args = parser.parse_args(argv)
    try:
        package, summary = run_nightly(
            output_root=args.output_root,
            authorization_note=args.authorization_note,
            limits=NightlyLimits(
                max_total_tokens=args.max_total_tokens,
                max_model_calls=args.max_model_calls,
                max_wall_seconds=args.max_wall_seconds,
            ),
        )
    except (NightlyError, ValueError) as error:
        print(f"nightly refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"package": str(package), "passed": summary["passed"]}, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
