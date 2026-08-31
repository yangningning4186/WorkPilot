"""Candidate → Validate → Backup → Atomic Final 的可信提交路径。"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.cowork.artifact_diff import build_artifact_diff
from app.cowork.artifact_lock import artifact_commit_lock
from app.cowork.artifact_manifest import (
    ArtifactClaimBinding,
    ArtifactManifest,
    ArtifactSkillRef,
    build_artifact_manifest,
)
from app.cowork.artifact_renderers import ArtifactSpec, render_candidate
from app.cowork.artifact_validation import ArtifactValidationReport, validate_artifact
from app.cowork.files import CoworkFileError, create_file_backup


@dataclass(frozen=True)
class ArtifactCommitResult:
    path: Path
    sha256: str
    size_bytes: int
    created: bool
    backup_path: Path | None
    diff: dict[str, object]
    manifest: ArtifactManifest
    validation: ArtifactValidationReport


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _render_validate_commit_locked(
    *,
    spec: ArtifactSpec,
    target: Path,
    baseline_sha256: str | None,
    skill: ArtifactSkillRef,
    evidence_bindings: list[ArtifactClaimBinding],
    max_bytes: int,
    backup_versions: int,
) -> ArtifactCommitResult:
    if not target.parent.is_dir() or target.parent.is_symlink():
        raise CoworkFileError("Artifact 目标父目录必须是已存在的普通目录")
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise CoworkFileError("Artifact 目标必须是普通文件，不能是符号链接或目录")
    created = not target.exists()
    if created and baseline_sha256 is not None:
        raise CoworkFileError("目标文件尚不存在，baseline_sha256 必须省略")
    if not created:
        if baseline_sha256 is None:
            raise CoworkFileError("覆盖现有 Artifact 必须提供读取后的 baseline_sha256")
        if _sha256(target) != baseline_sha256:
            raise CoworkFileError("Artifact 已在读取后发生变化，请重新读取后再生成")

    descriptor, raw_candidate = tempfile.mkstemp(
        prefix=f".{target.stem}.",
        suffix=f".candidate{target.suffix}",
        dir=target.parent,
    )
    os.close(descriptor)
    candidate = Path(raw_candidate)
    candidate.unlink()
    backup: Path | None = None
    try:
        render_candidate(spec, candidate)
        size = candidate.stat().st_size
        if size == 0 or size > max_bytes:
            raise CoworkFileError(f"候选 Artifact 大小 {size} bytes 不在允许范围内")
        bound_claim_ids = frozenset(
            binding.claim_id
            for binding in evidence_bindings
            if binding.evidence and not binding.missing_evidence_ids
        )
        report = validate_artifact(
            candidate,
            spec=spec,
            bound_claim_ids=bound_claim_ids,
            render_visual=True,
        )
        manifest = build_artifact_manifest(
            spec=spec,
            skill=skill,
            report=report,
            evidence_bindings=evidence_bindings,
            sandboxed_generated_code=False,
        )
        if not report.deliverable:
            detail = "；".join(report.quality.warnings[:5]) or "未知验证错误"
            raise CoworkFileError(f"候选 Artifact 验证失败，未覆盖目标文件：{detail}")
        if created:
            if target.exists():
                raise CoworkFileError("Artifact 目标在生成期间被创建，请重新检查目标")
        else:
            if _sha256(target) != baseline_sha256:
                raise CoworkFileError("Artifact 在候选验证期间发生变化，已停止提交")
            backup = create_file_backup(target, backup_versions)
            if _sha256(target) != baseline_sha256:
                raise CoworkFileError("Artifact 在备份期间发生变化，已停止提交")
        with candidate.open("rb") as stream:
            os.fsync(stream.fileno())
        if created:
            try:
                os.link(candidate, target)
            except FileExistsError as error:
                raise CoworkFileError("Artifact 目标在最终提交时被创建，未覆盖并发写入") from error
            candidate.unlink()
        else:
            os.replace(candidate, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
        return ArtifactCommitResult(
            path=target,
            sha256=_sha256(target),
            size_bytes=target.stat().st_size,
            created=created,
            backup_path=backup,
            diff=build_artifact_diff(
                after_path=target,
                before_path=backup,
                created=created,
            ),
            manifest=manifest,
            validation=report,
        )
    finally:
        candidate.unlink(missing_ok=True)


def render_validate_commit(
    *,
    spec: ArtifactSpec,
    target: Path,
    baseline_sha256: str | None,
    skill: ArtifactSkillRef,
    evidence_bindings: list[ArtifactClaimBinding],
    max_bytes: int,
    backup_versions: int,
) -> ArtifactCommitResult:
    if not target.parent.is_dir() or target.parent.is_symlink():
        raise CoworkFileError("Artifact 目标父目录必须是已存在的普通目录")
    with artifact_commit_lock(target):
        return _render_validate_commit_locked(
            spec=spec,
            target=target,
            baseline_sha256=baseline_sha256,
            skill=skill,
            evidence_bindings=evidence_bindings,
            max_bytes=max_bytes,
            backup_versions=backup_versions,
        )


__all__ = ["ArtifactCommitResult", "render_validate_commit"]
