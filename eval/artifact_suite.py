"""Deterministic Office artifact evaluation suites.

The suite evaluates the final files emitted by WorkPilot's fixed renderers.  It never
downloads benchmark data or calls a model; public datasets are adapted separately after
their license and split policy have been reviewed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from app.cowork.artifact_renderers import ArtifactSpec, render_candidate
from app.cowork.artifact_validation import ArtifactValidationReport
from eval.artifact_bench import score_artifact

SCHEMA_VERSION = "workpilot-artifact-eval-suite.v1"
PUBLIC_REGISTRY_SCHEMA_VERSION = "workpilot-public-artifact-benchmarks.v1"
DEFAULT_SUITE = Path(__file__).parent / "suites" / "artifact-rendering-dev-v1.json"
DEFAULT_PUBLIC_REGISTRY = (
    Path(__file__).parent / "datasets" / "artifact-benchmarks" / "catalog.json"
)

_ARTIFACT_ADAPTER: TypeAdapter[ArtifactSpec] = TypeAdapter(ArtifactSpec)
_FORMAT_SUFFIX = {
    "docx": ".docx",
    "html": ".html",
    "pdf": ".pdf",
    "pptx": ".pptx",
    "xlsx": ".xlsx",
}
_PLACEHOLDER = re.compile(r"\{\{fixture:([A-Za-z0-9._-]+)\}\}\Z")

ExpectedStage = Literal["passed", "schema", "render", "validation"]
ActualStage = Literal["passed", "schema", "render", "validation", "crash"]


class ArtifactSuiteError(ValueError):
    """The suite is ambiguous, unsafe, or internally inconsistent."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FixtureSpec(_StrictModel):
    path: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
    content: str = Field(max_length=1_000_000)


class ArtifactEvalItem(_StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,119}$")
    split: Literal["dev", "test"]
    artifact_type: Literal["docx", "html", "pdf", "pptx", "xlsx"]
    category: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,79}$")
    expected_stage: ExpectedStage = "passed"
    expected_detail_contains: str | None = Field(default=None, min_length=1, max_length=200)
    min_quality_score: int = Field(default=0, ge=0, le=100)
    required_checks: dict[str, Literal["passed", "warning", "failed", "not_run"]] = Field(
        default_factory=dict
    )
    fixtures: list[FixtureSpec] = Field(default_factory=list, max_length=20)
    spec: dict[str, Any]

    @model_validator(mode="after")
    def _consistent_case(self) -> ArtifactEvalItem:
        if self.spec.get("artifact_type") != self.artifact_type:
            raise ValueError("spec.artifact_type 必须与 item.artifact_type 一致")
        fixture_names = [fixture.path for fixture in self.fixtures]
        if len(fixture_names) != len(set(fixture_names)):
            raise ValueError("fixture path 必须唯一")
        if self.expected_stage == "passed" and not self.required_checks:
            raise ValueError("正向 case 必须声明 required_checks")
        if self.expected_stage != "passed" and not self.expected_detail_contains:
            raise ValueError("负向 case 必须声明 expected_detail_contains")
        return self


class ArtifactEvalSuite(_StrictModel):
    schema_version: Literal["workpilot-artifact-eval-suite.v1"]
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,119}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?$")
    origin: Literal["synthetic", "public", "human"]
    review_status: Literal["pending_human_review", "approved"]
    reviewer: str | None = Field(default=None, min_length=1, max_length=200)
    reviewed_at: str | None = Field(default=None, min_length=1, max_length=100)
    items: list[ArtifactEvalItem] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _review_and_ids(self) -> ArtifactEvalSuite:
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("item id 必须唯一")
        if self.review_status == "approved":
            if self.reviewer is None or self.reviewed_at is None:
                raise ValueError("approved suite 必须记录 reviewer 与 reviewed_at")
        elif self.reviewer is not None or self.reviewed_at is not None:
            raise ValueError("pending suite 不能预填 reviewer 或 reviewed_at")
        return self


class PublicBenchmarkLicense(_StrictModel):
    status: Literal["verified", "review_required"]
    spdx: str | None = Field(default=None, pattern=r"^[A-Za-z0-9.+-]+$")
    url: str | None = Field(default=None, pattern=r"^https://")

    @model_validator(mode="after")
    def _verified_has_identity(self) -> PublicBenchmarkLicense:
        if self.status == "verified" and (self.spdx is None or self.url is None):
            raise ValueError("verified license 必须包含 SPDX 与 URL")
        if self.status == "review_required" and self.spdx is not None:
            raise ValueError("license 未复核前不能预填 SPDX")
        return self


class PublicBenchmark(_StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,119}$")
    name: str = Field(min_length=1, max_length=200)
    artifact_types: list[Literal["docx", "pptx", "xlsx", "pdf", "html"]] = Field(min_length=1)
    task_types: list[
        Literal["generation", "editing", "comprehension", "workflow", "evaluation"]
    ] = Field(min_length=1)
    source_url: str = Field(pattern=r"^https://")
    paper_url: str | None = Field(default=None, pattern=r"^https://")
    license: PublicBenchmarkLicense
    integration_status: Literal["reference_only", "adapter_planned", "adapter_ready"]
    split_policy: str = Field(min_length=1, max_length=500)
    use_for: list[str] = Field(min_length=1, max_length=20)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class PublicBenchmarkRegistry(_StrictModel):
    schema_version: Literal["workpilot-public-artifact-benchmarks.v1"]
    benchmarks: list[PublicBenchmark] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _unique_ids(self) -> PublicBenchmarkRegistry:
        ids = [benchmark.id for benchmark in self.benchmarks]
        if len(ids) != len(set(ids)):
            raise ValueError("public benchmark id 必须唯一")
        return self


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactSuiteError(f"重复 JSON key: {key}")
        result[key] = value
    return result


def load_suite(path: Path = DEFAULT_SUITE) -> ArtifactEvalSuite:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        return ArtifactEvalSuite.model_validate(payload)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
    ) as error:
        raise ArtifactSuiteError(f"Artifact suite 无效：{error}") from error


def load_public_registry(
    path: Path = DEFAULT_PUBLIC_REGISTRY,
) -> PublicBenchmarkRegistry:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        return PublicBenchmarkRegistry.model_validate(payload)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
    ) as error:
        raise ArtifactSuiteError(f"公开 Artifact benchmark catalog 无效：{error}") from error


def suite_summary(suite: ArtifactEvalSuite) -> dict[str, object]:
    return {
        "name": suite.name,
        "version": suite.version,
        "origin": suite.origin,
        "review_status": suite.review_status,
        "items": len(suite.items),
        "splits": dict(sorted(Counter(item.split for item in suite.items).items())),
        "formats": dict(sorted(Counter(item.artifact_type for item in suite.items).items())),
        "categories": dict(sorted(Counter(item.category for item in suite.items).items())),
        "expected_stages": dict(
            sorted(Counter(item.expected_stage for item in suite.items).items())
        ),
    }


def _resolve_fixtures(value: object, fixture_paths: dict[str, Path]) -> object:
    if isinstance(value, str):
        match = _PLACEHOLDER.fullmatch(value)
        if match is None:
            return value
        try:
            return str(fixture_paths[match.group(1)])
        except KeyError as error:
            raise ArtifactSuiteError(f"引用了未声明 fixture：{match.group(1)}") from error
    if isinstance(value, list):
        return [_resolve_fixtures(item, fixture_paths) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_fixtures(item, fixture_paths) for key, item in value.items()}
    return value


def _checks(report: ArtifactValidationReport) -> dict[str, str]:
    result: dict[str, str] = {}
    for dimension_name in ("structural", "semantic", "visual", "evidence", "security"):
        dimension = getattr(report, dimension_name)
        result[f"{dimension_name}._status"] = dimension.status
        for check in dimension.checks:
            result[f"{dimension_name}.{check.name}"] = check.status
    return result


def _failed_report_detail(report: ArtifactValidationReport) -> str:
    if report.quality.warnings:
        return "；".join(report.quality.warnings)
    return "Artifact Validator 判定不可交付"


def _run_item(item: ArtifactEvalItem, output_root: Path) -> dict[str, object]:
    case_root = output_root / item.id
    case_root.mkdir(parents=True, exist_ok=False)
    fixture_paths: dict[str, Path] = {}
    for fixture in item.fixtures:
        path = case_root / fixture.path
        path.write_text(fixture.content, encoding="utf-8")
        fixture_paths[fixture.path] = path
    raw_spec = _resolve_fixtures(item.spec, fixture_paths)
    target = case_root / f"artifact{_FORMAT_SUFFIX[item.artifact_type]}"
    report: ArtifactValidationReport | None = None
    metrics: dict[str, float] | None = None
    actual_stage: ActualStage
    detail: str
    try:
        spec = _ARTIFACT_ADAPTER.validate_python(raw_spec)
    except ValidationError as error:
        actual_stage = "schema"
        detail = str(error)
    else:
        try:
            render_candidate(spec, target)
        except (OSError, RuntimeError, ValueError) as error:
            actual_stage = "render"
            detail = f"{type(error).__name__}: {error}"
        else:
            try:
                report, scored = score_artifact(target, spec=spec, render_visual=True)
            except Exception as error:
                actual_stage = "crash"
                detail = f"{type(error).__name__}: {error}"
            else:
                metrics = scored.public()
                actual_stage = "passed" if report.deliverable else "validation"
                detail = "deliverable" if report.deliverable else _failed_report_detail(report)

    stage_matches = actual_stage == item.expected_stage
    detail_matches = (
        item.expected_detail_contains is None
        or item.expected_detail_contains.casefold() in detail.casefold()
    )
    check_failures: list[str] = []
    check_statuses = _checks(report) if report is not None else {}
    for name, expected in item.required_checks.items():
        actual = check_statuses.get(name)
        if actual != expected:
            check_failures.append(f"{name}: expected={expected}, actual={actual or '<missing>'}")
    quality_matches = report is None or report.quality.score >= item.min_quality_score
    passed = stage_matches and detail_matches and not check_failures and quality_matches
    return {
        "id": item.id,
        "split": item.split,
        "artifact_type": item.artifact_type,
        "category": item.category,
        "passed": passed,
        "expected_stage": item.expected_stage,
        "actual_stage": actual_stage,
        "detail": detail,
        "quality_score": report.quality.score if report is not None else None,
        "min_quality_score": item.min_quality_score,
        "check_failures": check_failures,
        "metrics": metrics,
        "artifact": str(target) if target.is_file() else None,
    }


def run_suite(suite: ArtifactEvalSuite, output_root: Path) -> dict[str, object]:
    if output_root.exists():
        raise ArtifactSuiteError("output_root 已存在；评测不覆盖既有结果")
    output_root.mkdir(parents=True)
    results = [_run_item(item, output_root) for item in suite.items]
    passed = sum(bool(result["passed"]) for result in results)
    format_totals = Counter(str(result["artifact_type"]) for result in results)
    format_passes = Counter(str(result["artifact_type"]) for result in results if result["passed"])
    payload: dict[str, object] = {
        "schema_version": 1,
        "suite": suite_summary(suite),
        "summary": {
            "passed": passed,
            "failed": len(results) - passed,
            "pass_rate": passed / len(results),
            "by_format": {
                name: {
                    "passed": format_passes[name],
                    "items": count,
                    "pass_rate": format_passes[name] / count,
                }
                for name, count in sorted(format_totals.items())
            },
        },
        "results": results,
    }
    (output_root / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate or run a WorkPilot artifact eval suite")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    public_parser = subparsers.add_parser("public-catalog")
    public_parser.add_argument("--catalog", type=Path, default=DEFAULT_PUBLIC_REGISTRY)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    run_parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "public-catalog":
            registry = load_public_registry(args.catalog)
            print(
                json.dumps(
                    {
                        "benchmarks": len(registry.benchmarks),
                        "adapter_ready": sum(
                            item.integration_status == "adapter_ready"
                            for item in registry.benchmarks
                        ),
                        "license_review_required": sum(
                            item.license.status == "review_required" for item in registry.benchmarks
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        suite = load_suite(args.suite)
        if args.command == "validate":
            print(json.dumps(suite_summary(suite), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        report = run_suite(suite, args.output_dir)
    except ArtifactSuiteError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["summary"]["failed"] == 0 else 1  # type: ignore[index]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())


__all__ = [
    "DEFAULT_PUBLIC_REGISTRY",
    "DEFAULT_SUITE",
    "ArtifactEvalSuite",
    "ArtifactSuiteError",
    "load_public_registry",
    "load_suite",
    "run_suite",
    "suite_summary",
]
