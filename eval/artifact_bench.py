"""ArtifactBench v1：直接评最终文件，而不是只评模型文字回答。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.cowork.artifact_manifest import ArtifactClaimBinding
from app.cowork.artifact_renderers.contracts import ArtifactSpec
from app.cowork.artifact_validation import ArtifactValidationReport, validate_artifact


@dataclass(frozen=True)
class ArtifactBenchMetrics:
    file_open_rate: float
    requirement_pass: float
    visual_validation_pass: float
    formula_validity: float
    evidence_coverage: float
    citation_accuracy: float
    unsafe_active_content_rate: float
    conflict_detection: float
    duplicate_side_effect_rate: float

    def public(self) -> dict[str, float]:
        return asdict(self)


def _check(
    report: ArtifactValidationReport, name: str
) -> tuple[str, int | float | str | bool | None] | None:
    for dimension_name in ("structural", "semantic", "visual", "evidence", "security"):
        dimension = getattr(report, dimension_name)
        for item in dimension.checks:
            if item.name == name:
                return item.status, item.value
    return None


def score_artifact(
    path: Path,
    *,
    spec: ArtifactSpec | None = None,
    evidence_bindings: list[ArtifactClaimBinding] | None = None,
    render_visual: bool = True,
    conflict_checked: bool = False,
    conflict_detected: bool = False,
    side_effect_attempts: int = 1,
    unique_effects: int = 1,
) -> tuple[ArtifactValidationReport, ArtifactBenchMetrics]:
    bindings = evidence_bindings or []
    bound_claim_ids = frozenset(
        item.claim_id
        for item in bindings
        if item.evidence and not item.missing_evidence_ids
    )
    report = validate_artifact(
        path,
        spec=spec,
        bound_claim_ids=bound_claim_ids,
        render_visual=render_visual,
    )
    evidence_check = _check(report, "claim_coverage")
    evidence_coverage = (
        float(evidence_check[1]) / 100
        if evidence_check is not None and isinstance(evidence_check[1], (int, float))
        else 1.0
    )
    formula_check = _check(report, "formulas")
    formula_validity = (
        1.0 if formula_check is None or formula_check[0] == "passed" else 0.0
    )
    claim_count = len(spec.claims) if spec is not None else 0
    accurate = sum(
        bool(item.evidence) and not item.missing_evidence_ids for item in bindings
    )
    citation_accuracy = 1.0 if claim_count == 0 else accurate / claim_count
    duplicates = max(0, side_effect_attempts - unique_effects)
    duplicate_rate = (
        duplicates / side_effect_attempts if side_effect_attempts > 0 else 0.0
    )
    metrics = ArtifactBenchMetrics(
        file_open_rate=1.0 if report.structural.status == "passed" else 0.0,
        requirement_pass=1.0 if report.semantic.status != "failed" else 0.0,
        visual_validation_pass=1.0 if report.visual.status == "passed" else 0.0,
        formula_validity=formula_validity,
        evidence_coverage=evidence_coverage,
        citation_accuracy=citation_accuracy,
        unsafe_active_content_rate=1.0 if report.security.status == "failed" else 0.0,
        conflict_detection=1.0 if conflict_checked and conflict_detected else 0.0,
        duplicate_side_effect_rate=duplicate_rate,
    )
    return report, metrics


def artifact_bench_record(path: Path, **kwargs: Any) -> dict[str, Any]:
    report, metrics = score_artifact(path, **kwargs)
    return {
        "schema_version": 1,
        "artifact": str(path),
        "artifact_type": report.artifact_type,
        "validation": report.model_dump(mode="json"),
        "metrics": metrics.public(),
    }


__all__ = ["ArtifactBenchMetrics", "artifact_bench_record", "score_artifact"]
