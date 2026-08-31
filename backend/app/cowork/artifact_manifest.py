"""ArtifactManifest v1 与 Claim → Evidence 绑定。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.cowork.artifact_renderers.contracts import ArtifactSpec
from app.cowork.artifact_validation import ArtifactValidationReport


class _StrictManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactSkillRef(_StrictManifest):
    name: str
    origin: Literal["builtin", "user", "project", "unknown"]
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    kind: Literal["planning", "artifact", "workflow", "action", "unknown"] = "unknown"


class ArtifactRuntimeRef(_StrictManifest):
    profile: str
    sandboxed_generated_code: bool
    renderer: str


class ArtifactEvidenceRef(_StrictManifest):
    citation_id: str
    kind: str | None = None
    title: str | None = None
    source_uri: str | None = None
    quote: str | None = None
    locator: int | None = None
    locations: list[dict[str, Any]] = Field(default_factory=list)


class ArtifactClaimBinding(_StrictManifest):
    claim_id: str
    claim: str
    target_type: str
    target_id: str
    evidence: list[ArtifactEvidenceRef]
    missing_evidence_ids: list[str] = Field(default_factory=list)


class ArtifactManifest(_StrictManifest):
    schema_version: Literal[1] = 1
    status: Literal["candidate", "validated", "failed"]
    artifact_type: Literal["docx", "xlsx", "pptx", "pdf", "html"]
    skill: ArtifactSkillRef
    runtime: ArtifactRuntimeRef
    validation: dict[str, str]
    validation_report: ArtifactValidationReport
    quality: dict[str, Any]
    evidence_bindings: list[ArtifactClaimBinding] = Field(default_factory=list)


def bind_claim_evidence(
    spec: ArtifactSpec,
    ledger: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> list[ArtifactClaimBinding]:
    by_id = {
        str(item.get("citation_id")): item
        for item in ledger
        if isinstance(item.get("citation_id"), (str, int))
    }
    bindings: list[ArtifactClaimBinding] = []
    for claim in spec.claims:
        evidence: list[ArtifactEvidenceRef] = []
        missing: list[str] = []
        for evidence_id in claim.evidence_ids:
            raw = by_id.get(evidence_id)
            if raw is None:
                missing.append(evidence_id)
                continue
            raw_locations = raw.get("locations")
            evidence.append(
                ArtifactEvidenceRef(
                    citation_id=evidence_id,
                    kind=str(raw["kind"]) if raw.get("kind") is not None else None,
                    title=str(raw["title"]) if raw.get("title") is not None else None,
                    source_uri=(
                        str(raw["source_uri"]) if raw.get("source_uri") is not None else None
                    ),
                    quote=str(raw["quote"])[:2_000] if raw.get("quote") is not None else None,
                    locator=raw.get("locator") if isinstance(raw.get("locator"), int) else None,
                    locations=(
                        [dict(item) for item in raw_locations if isinstance(item, dict)][:20]
                        if isinstance(raw_locations, list)
                        else []
                    ),
                )
            )
        bindings.append(
            ArtifactClaimBinding(
                claim_id=claim.claim_id,
                claim=claim.text,
                target_type=claim.target_type,
                target_id=claim.target_id,
                evidence=evidence,
                missing_evidence_ids=missing,
            )
        )
    return bindings


def build_artifact_manifest(
    *,
    spec: ArtifactSpec,
    skill: ArtifactSkillRef,
    report: ArtifactValidationReport,
    evidence_bindings: list[ArtifactClaimBinding],
    sandboxed_generated_code: bool,
) -> ArtifactManifest:
    states = {
        name: getattr(report, name).status
        for name in ("structural", "semantic", "visual", "evidence", "security")
    }
    return ArtifactManifest(
        status="validated" if report.deliverable else "failed",
        artifact_type=spec.artifact_type,
        skill=skill,
        runtime=ArtifactRuntimeRef(
            profile="artifact-python",
            sandboxed_generated_code=sandboxed_generated_code,
            renderer=f"workpilot-{spec.artifact_type}-renderer-v1",
        ),
        validation=states,
        validation_report=report,
        quality=report.quality.model_dump(mode="json"),
        evidence_bindings=evidence_bindings,
    )


def legacy_artifact_manifest(
    *,
    artifact_type: Literal["docx", "xlsx", "pptx", "pdf", "html"],
    report: ArtifactValidationReport,
    sandboxed_generated_code: bool,
) -> ArtifactManifest:
    states = {
        name: getattr(report, name).status
        for name in ("structural", "semantic", "visual", "evidence", "security")
    }
    return ArtifactManifest(
        status="validated" if report.deliverable else "failed",
        artifact_type=artifact_type,
        skill=ArtifactSkillRef(name=artifact_type, origin="unknown"),
        runtime=ArtifactRuntimeRef(
            profile="artifact-python",
            sandboxed_generated_code=sandboxed_generated_code,
            renderer="external-or-generated",
        ),
        validation=states,
        validation_report=report,
        quality=report.quality.model_dump(mode="json"),
    )


__all__ = [
    "ArtifactClaimBinding",
    "ArtifactManifest",
    "ArtifactSkillRef",
    "bind_claim_evidence",
    "build_artifact_manifest",
    "legacy_artifact_manifest",
]
