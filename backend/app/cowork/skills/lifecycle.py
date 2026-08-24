"""Skill 安装、更新、启停、资源与卸载生命周期。

**只有 user 层可写。** builtin 随代码发布，安装位可能整个是只读的，而且下次升级会被
原样替换——所以对它只开放两件事：停用（标记写在 user 层）与 fork（装一个同名 user
技能把它盖住）。删除 builtin 一律拒绝，并在错误信息里告诉模型这两条可行的路。
"""

from __future__ import annotations

import base64
import binascii
import os
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import IO, Any

from app.cowork.skills.catalog import (
    BUILTIN_DISABLED_DIRNAME,
    BUILTIN_SKILLS_ROOT,
    SkillCatalogError,
    SkillOrigin,
    builtin_disabled_names,
    load_skill_file,
)


@dataclass(frozen=True)
class ManagedSkill:
    name: str
    enabled: bool
    description: str | None
    sha256: str | None
    resources: tuple[str, ...]
    error: str | None
    origin: SkillOrigin = "user"
    #: builtin 被同名 user 技能盖住时为 True。此时列表里两条都在，界面才能解释
    #: "为什么我改了出厂那份却没有生效"。
    shadowed: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "description": self.description,
            "sha256": self.sha256,
            "resources": list(self.resources),
            "error": self.error,
            "origin": self.origin,
            "shadowed": self.shadowed,
            # 界面据此决定按钮：出厂技能只能停用或 fork，不能删。
            "removable": self.origin == "user",
        }


def _scan_managed(
    resolved: Path,
    *,
    origin: SkillOrigin,
    max_files: int,
    max_bytes: int,
    enabled_for: Callable[[Path], bool],
) -> list[ManagedSkill]:
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
            skill = load_skill_file(child / "SKILL.md", max_bytes=max_bytes, origin=origin)
        except (OSError, UnicodeError, SkillCatalogError) as error:
            result.append(
                ManagedSkill(child.name, False, None, None, resources, str(error), origin)
            )
        else:
            result.append(
                ManagedSkill(
                    child.name,
                    enabled_for(child),
                    skill.description,
                    skill.sha256,
                    resources,
                    None,
                    origin,
                )
            )
    return result


def list_managed_skills(
    root: Path,
    *,
    max_files: int,
    max_bytes: int,
    builtin_root: Path | None = BUILTIN_SKILLS_ROOT,
) -> list[ManagedSkill]:
    """两层都列出来，出厂的排在前面。

    被盖住的 builtin **仍然列出**（标 ``shadowed``），因为把它从列表里拿掉之后，
    "我 fork 过它" 这件事就只剩一条看不出来源的 user 记录了。
    """

    resolved = _safe_root(root)
    user = _scan_managed(
        resolved,
        origin="user",
        max_files=max_files,
        max_bytes=max_bytes,
        enabled_for=lambda child: not (child / ".disabled").exists(),
    )
    if builtin_root is None:
        return user
    user_names = {item.name for item in user}
    disabled = builtin_disabled_names(resolved)
    builtin = [
        replace(item, shadowed=item.name in user_names)
        for item in _scan_managed(
            _safe_root(builtin_root),
            origin="builtin",
            max_files=max_files,
            max_bytes=max_bytes,
            enabled_for=lambda child: child.name not in disabled,
        )
    ]
    return builtin + user


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


def install_auto_distilled_skill(
    root: Path,
    *,
    name: str,
    capability_key: str,
    skill_md: str,
    max_bytes: int,
    builtin_root: Path | None = BUILTIN_SKILLS_ROOT,
) -> ManagedSkill:
    """幂等安装自动蒸馏 Skill，但绝不覆盖同名人工 Skill，也不遮蔽出厂 Skill。"""

    resolved = _safe_root(root)
    if builtin_root is not None:
        builtin_dir = _skill_dir(_safe_root(builtin_root), name)
        if builtin_dir.is_dir() and not builtin_dir.is_symlink():
            # fork 出厂技能是**用户**的决定。让自动蒸馏拿走这个名字，出厂那份就被
            # 一段没人审过的正文悄悄换掉了，而列表里看上去只是"多了一条 learned-*"。
            raise FileExistsError(f"同名出厂 Skill 已存在，自动晋升未遮蔽: {name}")
    target = _skill_dir(resolved, name)
    replace = target.exists()
    if replace:
        existing = target / "SKILL.md"
        try:
            raw = existing.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise FileExistsError(f"同名 Skill 已存在且无法确认来源: {name}") from error
        markers = ("origin: auto_distilled", f"capability_key: {capability_key}")
        if not all(marker in raw for marker in markers):
            raise FileExistsError(f"同名人工 Skill 已存在，自动晋升未覆盖: {name}")
    return install_skill(
        resolved,
        name=name,
        skill_md=skill_md,
        enabled=True,
        max_bytes=max_bytes,
        replace=replace,
    )


def _write_marker(marker: Path, body: str) -> None:
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(body)


def set_skill_enabled(
    root: Path,
    *,
    name: str,
    enabled: bool,
    max_bytes: int,
    builtin_root: Path | None = BUILTIN_SKILLS_ROOT,
) -> ManagedSkill:
    """user 层写目录内的 ``.disabled``；builtin 写 user 层的停用标记。

    先看 user 层：同名 fork 存在时开关管的就是那份 fork——它才是实际生效的那一个，
    去切 builtin 的开关会得到"点了没反应"。
    """

    resolved = _safe_root(root)
    target = _skill_dir(resolved, name)
    if target.is_dir() and not target.is_symlink():
        marker = target / ".disabled"
        if enabled:
            marker.unlink(missing_ok=True)
        else:
            _write_marker(marker, "disabled\n")
        return _managed_one(target, max_bytes=max_bytes, origin="user")
    if builtin_root is not None:
        builtin_dir = _skill_dir(_safe_root(builtin_root), name)
        if builtin_dir.is_dir() and not builtin_dir.is_symlink():
            marker_root = resolved / BUILTIN_DISABLED_DIRNAME
            marker_root.mkdir(parents=True, exist_ok=True)
            marker = marker_root / name
            if enabled:
                marker.unlink(missing_ok=True)
            else:
                _write_marker(marker, "disabled\n")
            return replace(
                _managed_one(builtin_dir, max_bytes=max_bytes, origin="builtin"),
                enabled=enabled,
            )
    raise FileNotFoundError(f"Skill 不存在: {name}")


def remove_skill(
    root: Path,
    *,
    name: str,
    builtin_root: Path | None = BUILTIN_SKILLS_ROOT,
) -> None:
    """只删 user 层。删掉一份 fork 会让同名 builtin 重新生效，这是刻意的回退路径。"""

    resolved = _safe_root(root)
    target = _skill_dir(resolved, name)
    if not target.is_dir() or target.is_symlink():
        if builtin_root is not None:
            builtin_dir = _skill_dir(_safe_root(builtin_root), name)
            if builtin_dir.is_dir() and not builtin_dir.is_symlink():
                raise SkillCatalogError(
                    f"{name} 是随产品出厂的 Skill，不能删除。"
                    "要停用它，把 enabled 设为 false；"
                    "要改它的流程，用同名安装一份自己的版本覆盖（删掉那份即可恢复出厂）。"
                )
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
                    _copy_zip_member_bounded(
                        archive_source,
                        archive_target,
                        member_name=member.filename,
                        max_bytes=max_bytes,
                    )
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


def _copy_zip_member_bounded(
    source: IO[bytes],
    target: IO[bytes],
    *,
    member_name: str,
    max_bytes: int,
) -> int:
    """按实际解压字节计数；ZIP 中央目录的 file_size 只用于快速拒绝。"""

    copied = 0
    while chunk := source.read(min(64 * 1024, max_bytes - copied + 1)):
        copied += len(chunk)
        if copied > max_bytes:
            raise SkillCatalogError(f"Skill ZIP 文件实际解压超过大小上限: {member_name}")
        target.write(chunk)
    return copied


def read_skill_resource(
    root: Path,
    *,
    name: str,
    resource: str,
    max_bytes: int,
    builtin_root: Path | None = BUILTIN_SKILLS_ROOT,
) -> tuple[str, str]:
    skill_dir = _skill_dir(_safe_root(root), name)
    if not skill_dir.is_dir() and builtin_root is not None:
        # user 层没有这份技能时才回落出厂层；顺序和目录合并保持一致，否则
        # fork 过的技能会读到出厂那份的资源。
        skill_dir = _skill_dir(_safe_root(builtin_root), name)
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


def read_skill_definition_resource(
    skill_path: Path,
    *,
    resource: str,
    max_bytes: int,
) -> tuple[str, str]:
    """按 catalog 已选中的具体 SKILL.md 读资源，保持 project > user > builtin。"""

    relative = PurePosixPath(resource)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise SkillCatalogError("Skill resource 路径非法")
    if skill_path.name != "SKILL.md" or not skill_path.is_file() or skill_path.is_symlink():
        raise FileNotFoundError(skill_path)
    skill_dir = skill_path.parent.resolve(strict=True)
    try:
        target = skill_dir.joinpath(*relative.parts).resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise FileNotFoundError(resource) from error
    if not target.is_relative_to(skill_dir) or not target.is_file() or target.is_symlink():
        raise FileNotFoundError(resource)
    if target.stat().st_size > max_bytes:
        raise SkillCatalogError("Skill resource 超过读取上限")
    return target.read_text(encoding="utf-8"), relative.as_posix()


def _managed_one(target: Path, *, max_bytes: int, origin: SkillOrigin = "user") -> ManagedSkill:
    skill = load_skill_file(target / "SKILL.md", max_bytes=max_bytes, origin=origin)
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
        origin,
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
