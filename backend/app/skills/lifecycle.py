"""Skill 安装、更新、启停、资源与卸载生命周期。"""

from __future__ import annotations

import base64
import binascii
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from app.skills.catalog import SkillCatalogError, load_skill_file


@dataclass(frozen=True)
class ManagedSkill:
    name: str
    enabled: bool
    description: str | None
    sha256: str | None
    resources: tuple[str, ...]
    error: str | None

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "description": self.description,
            "sha256": self.sha256,
            "resources": list(self.resources),
            "error": self.error,
        }


def list_managed_skills(root: Path, *, max_files: int, max_bytes: int) -> list[ManagedSkill]:
    resolved = _safe_root(root)
    if not resolved.exists():
        return []
    result: list[ManagedSkill] = []
    for index, child in enumerate(sorted(resolved.iterdir(), key=lambda item: item.name)):
        if index >= max_files:
            break
        if not child.is_dir() or child.is_symlink() or not (child / "SKILL.md").is_file():
            continue
        resources = tuple(
            path.relative_to(child).as_posix()
            for path in sorted(child.rglob("*"))
            if path.is_file()
            and not path.is_symlink()
            and path.name not in {"SKILL.md", ".disabled"}
        )
        try:
            skill = load_skill_file(child / "SKILL.md", max_bytes=max_bytes)
        except (OSError, UnicodeError, SkillCatalogError) as error:
            result.append(
                ManagedSkill(child.name, False, None, None, resources, str(error))
            )
        else:
            result.append(
                ManagedSkill(
                    child.name,
                    not (child / ".disabled").exists(),
                    skill.description,
                    skill.sha256,
                    resources,
                    None,
                )
            )
    return result


def install_skill(
    root: Path,
    *,
    name: str,
    skill_md: str,
    enabled: bool,
    max_bytes: int,
    replace: bool,
) -> ManagedSkill:
    resolved = _safe_root(root)
    resolved.mkdir(parents=True, exist_ok=True)
    target = _skill_dir(resolved, name)
    if target.exists() and not replace:
        raise FileExistsError(f"Skill 已存在: {name}")
    encoded = skill_md.encode("utf-8")
    if not encoded or len(encoded) > max_bytes:
        raise SkillCatalogError(f"SKILL.md 必须位于 1 到 {max_bytes} bytes")
    staging = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=resolved))
    final_staging = staging / name
    try:
        final_staging.mkdir()
        candidate = final_staging / "SKILL.md"
        candidate.write_bytes(encoded)
        load_skill_file(candidate, max_bytes=max_bytes)
        if target.is_dir() and not target.is_symlink():
            for source in target.iterdir():
                if source.name in {"SKILL.md", ".disabled"} or source.is_symlink():
                    continue
                destination = final_staging / source.name
                if source.is_dir():
                    shutil.copytree(source, destination)
                elif source.is_file():
                    shutil.copy2(source, destination)
        if not enabled:
            (final_staging / ".disabled").write_text("disabled\n", encoding="utf-8")
        if target.exists():
            backup = resolved / f".{name}.previous"
            if backup.exists():
                shutil.rmtree(backup)
            os.replace(target, backup)
            try:
                os.replace(final_staging, target)
            except Exception:
                os.replace(backup, target)
                raise
            shutil.rmtree(backup)
        else:
            os.replace(final_staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return _managed_one(target, max_bytes=max_bytes)


def set_skill_enabled(root: Path, *, name: str, enabled: bool, max_bytes: int) -> ManagedSkill:
    target = _skill_dir(_safe_root(root), name)
    if not target.is_dir() or target.is_symlink():
        raise FileNotFoundError(f"Skill 不存在: {name}")
    marker = target / ".disabled"
    if enabled:
        marker.unlink(missing_ok=True)
    else:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("disabled\n")
    return _managed_one(target, max_bytes=max_bytes)


def remove_skill(root: Path, *, name: str) -> None:
    resolved = _safe_root(root)
    target = _skill_dir(resolved, name)
    if not target.is_dir() or target.is_symlink():
        raise FileNotFoundError(f"Skill 不存在: {name}")
    shutil.rmtree(target)


def import_skill_zip(
    root: Path,
    *,
    archive_base64: str,
    enabled: bool,
    max_bytes: int,
) -> ManagedSkill:
    try:
        archive = base64.b64decode(archive_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SkillCatalogError("Skill ZIP 不是有效 base64") from error
    if len(archive) > max_bytes * 8:
        raise SkillCatalogError("Skill ZIP 超过大小上限")
    root_path = _safe_root(root)
    root_path.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".skill-import.", dir=root_path))
    try:
        archive_path = staging / "skill.zip"
        archive_path.write_bytes(archive)
        extracted = staging / "extracted"
        extracted.mkdir()
        with zipfile.ZipFile(archive_path) as bundle:
            members = bundle.infolist()
            if len(members) > 500:
                raise SkillCatalogError("Skill ZIP 文件数超过 500")
            for member in members:
                archive_relative = PurePosixPath(member.filename)
                if (
                    archive_relative.is_absolute()
                    or ".." in archive_relative.parts
                    or member.is_dir()
                ):
                    continue
                if member.file_size > max_bytes:
                    raise SkillCatalogError(f"Skill ZIP 文件过大: {member.filename}")
                target = extracted.joinpath(*archive_relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as archive_source, target.open("wb") as archive_target:
                    shutil.copyfileobj(archive_source, archive_target)
        candidates = list(extracted.rglob("SKILL.md"))
        if len(candidates) != 1:
            raise SkillCatalogError("Skill ZIP 必须且只能包含一个 SKILL.md")
        skill = load_skill_file(candidates[0], max_bytes=max_bytes)
        install_skill(
            root_path,
            name=skill.name,
            skill_md=candidates[0].read_text(encoding="utf-8"),
            enabled=enabled,
            max_bytes=max_bytes,
            replace=False,
        )
        target_dir = _skill_dir(root_path, skill.name)
        source_dir = candidates[0].parent
        for source in source_dir.rglob("*"):
            if not source.is_file() or source.name == "SKILL.md" or source.is_symlink():
                continue
            resource_relative = source.relative_to(source_dir)
            resource_target = target_dir / resource_relative
            resource_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, resource_target)
        return _managed_one(target_dir, max_bytes=max_bytes)
    except zipfile.BadZipFile as error:
        raise SkillCatalogError("Skill ZIP 损坏") from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def read_skill_resource(
    root: Path, *, name: str, resource: str, max_bytes: int
) -> tuple[str, str]:
    skill_dir = _skill_dir(_safe_root(root), name)
    relative = PurePosixPath(resource)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise SkillCatalogError("Skill resource 路径非法")
    if not skill_dir.is_dir() or skill_dir.is_symlink():
        raise FileNotFoundError(name)
    resolved_skill_dir = skill_dir.resolve(strict=True)
    candidate = skill_dir.joinpath(*relative.parts)
    try:
        target = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise FileNotFoundError(resource) from error
    if not target.is_relative_to(resolved_skill_dir) or not target.is_file():
        raise FileNotFoundError(resource)
    if target.stat().st_size > max_bytes:
        raise SkillCatalogError("Skill resource 超过读取上限")
    return target.read_text(encoding="utf-8"), relative.as_posix()


def _managed_one(target: Path, *, max_bytes: int) -> ManagedSkill:
    skill = load_skill_file(target / "SKILL.md", max_bytes=max_bytes)
    resources = tuple(
        path.relative_to(target).as_posix()
        for path in sorted(target.rglob("*"))
        if path.is_file() and path.name not in {"SKILL.md", ".disabled"} and not path.is_symlink()
    )
    return ManagedSkill(
        skill.name,
        not (target / ".disabled").exists(),
        skill.description,
        skill.sha256,
        resources,
        None,
    )


def _safe_root(root: Path) -> Path:
    expanded = root.expanduser()
    if expanded.exists() and (not expanded.is_dir() or expanded.is_symlink()):
        raise SkillCatalogError("Skill 根目录必须是普通目录")
    return expanded.resolve()


def _skill_dir(root: Path, name: str) -> Path:
    if (
        not 1 <= len(name) <= 64
        or name[0] not in "abcdefghijklmnopqrstuvwxyz0123456789"
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in name)
    ):
        raise SkillCatalogError("Skill name 非法")
    target = (root / name).resolve(strict=False)
    if target.parent != root:
        raise SkillCatalogError("Skill 路径越界")
    return target
