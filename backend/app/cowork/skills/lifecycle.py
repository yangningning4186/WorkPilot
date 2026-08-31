"""Skill 安装、更新、启停、资源与卸载生命周期。

**只有 user 层可写。** builtin 随代码发布，安装位可能整个是只读的，而且下次升级会被
原样替换——所以对它只开放两件事：停用（标记写在 user 层）与 fork（装一个同名 user
技能把它盖住）。删除 builtin 一律拒绝，并在错误信息里告诉模型这两条可行的路。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import IO, Any

from app.cowork.skills.candidate_store import skill_persistence_skip_reason
from app.cowork.skills.catalog import (
    BUILTIN_DISABLED_DIRNAME,
    BUILTIN_SKILLS_ROOT,
    SkillCatalogError,
    SkillKind,
    SkillOrigin,
    disabled_skill_names,
    load_skill_file,
)

AUTO_DISTILLED_PROVENANCE_PURPOSE = "skill-auto-distillation-provenance-v1"
_AUTO_DISTILLED_RECEIPT = ".auto-distilled.json"
_INTERNAL_SKILL_FILES = frozenset({_AUTO_DISTILLED_RECEIPT})
_AUTO_RECEIPT_MAX_BYTES = 4 * 1024
_IGNORED_RESOURCE_DIRS = frozenset({"__pycache__", "dist", "node_modules"})


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
    kind: SkillKind = "workflow"
    runtime_profile: str = "none"
    compatibility: tuple[str, ...] = ()

    def resource_counts(self) -> dict[str, int]:
        counts = {"references": 0, "scripts": 0, "assets": 0, "evals": 0, "other": 0}
        for resource in self.resources:
            group = resource.partition("/")[0]
            counts[group if group in counts else "other"] += 1
        return counts

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
            "kind": self.kind,
            "runtime_profile": self.runtime_profile,
            "compatibility": list(self.compatibility),
            "resource_counts": self.resource_counts(),
            # 界面据此决定按钮：出厂技能只能停用或 fork，不能删。
            "removable": self.origin == "user",
        }


@dataclass(frozen=True)
class SkillResource:
    path: str
    size_bytes: int
    readable: bool

    def public(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "readable": self.readable,
        }


def list_skill_resources(
    skill_file: Path,
    *,
    max_files: int,
    max_bytes: int,
) -> tuple[SkillResource, ...]:
    """枚举一份已选中 Skill 的普通资源文件，不跟随任何符号链接。

    ``max_files`` 同时限制目录项扫描量：大量空目录也不能把一次 ``load_skill`` 变成
    无界遍历。超过单文件读取上限的资源仍列出，但明确标为不可读。
    """

    if max_files < 1 or max_bytes < 1:
        raise SkillCatalogError("Skill resource 上限必须为正数")
    skill_dir = _skill_directory_from_file(skill_file)
    pending = [skill_dir]
    scanned = 0
    resources: list[SkillResource] = []
    try:
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
            child_directories: list[Path] = []
            for entry in entries:
                scanned += 1
                if scanned > max_files:
                    raise SkillCatalogError(f"Skill resource 目录项超过扫描上限 {max_files}")
                if entry.is_symlink():
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in _IGNORED_RESOURCE_DIRS:
                        child_directories.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False) or entry.name in {
                    "SKILL.md",
                    ".disabled",
                    *_INTERNAL_SKILL_FILES,
                }:
                    continue
                relative = path.relative_to(skill_dir).as_posix()
                size = entry.stat(follow_symlinks=False).st_size
                resources.append(
                    SkillResource(path=relative, size_bytes=size, readable=size <= max_bytes)
                )
            # 反向压栈，让字典序靠前的目录先扫描；最终仍排序，输出与文件系统遍历顺序无关。
            pending.extend(reversed(child_directories))
    except SkillCatalogError:
        raise
    except OSError as error:
        raise SkillCatalogError("Skill resource 目录无法安全枚举") from error
    return tuple(sorted(resources, key=lambda item: item.path))


def resolve_skill_resource_path(skill_file: Path, *, resource: str) -> tuple[Path, str]:
    """解析资源但不读取内容，供 project Skill 在读取前完成目录授权。"""

    skill_dir = _skill_directory_from_file(skill_file)
    relative = PurePosixPath(resource)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or relative.name in {"SKILL.md", ".disabled", *_INTERNAL_SKILL_FILES}
    ):
        raise SkillCatalogError("Skill resource 路径非法")
    candidate = skill_dir.joinpath(*relative.parts)
    current = skill_dir
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise FileNotFoundError(resource)
    try:
        target = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise FileNotFoundError(resource) from error
    if not target.is_relative_to(skill_dir) or not target.is_file():
        raise FileNotFoundError(resource)
    return target, relative.as_posix()


def read_skill_resource_file(
    skill_file: Path,
    *,
    resource: str,
    max_bytes: int,
) -> tuple[str, str]:
    """按实际读取字节限制文本资源，避免 stat/read 竞态绕过大小边界。"""

    target, normalized = resolve_skill_resource_path(skill_file, resource=resource)
    try:
        with target.open("rb") as stream:
            content = stream.read(max_bytes + 1)
    except OSError as error:
        raise FileNotFoundError(resource) from error
    if len(content) > max_bytes:
        raise SkillCatalogError("Skill resource 超过读取上限")
    try:
        return content.decode("utf-8"), normalized
    except UnicodeDecodeError as error:
        raise SkillCatalogError("Skill resource 不是 UTF-8 文本") from error


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
        resources_list: list[str] = []
        for directory, dirnames, filenames in os.walk(child, followlinks=False):
            directory_path = Path(directory)
            dirnames[:] = [
                name
                for name in sorted(dirnames)
                if name not in _IGNORED_RESOURCE_DIRS
                and not (directory_path / name).is_symlink()
            ]
            for filename in sorted(filenames):
                path = directory_path / filename
                if (
                    filename in {"SKILL.md", ".disabled", *_INTERNAL_SKILL_FILES}
                    or path.is_symlink()
                    or not path.is_file()
                ):
                    continue
                resources_list.append(path.relative_to(child).as_posix())
        resources = tuple(sorted(resources_list))
        try:
            skill = load_skill_file(child / "SKILL.md", max_bytes=max_bytes, origin=origin)
        except (OSError, UnicodeError, SkillCatalogError) as error:
            result.append(
                ManagedSkill(child.name, False, None, None, resources, str(error), origin)
            )
        else:
            result.append(
                ManagedSkill(
                    name=child.name,
                    enabled=enabled_for(child),
                    description=skill.description,
                    sha256=skill.sha256,
                    resources=resources,
                    error=None,
                    origin=origin,
                    kind=skill.kind,
                    runtime_profile=skill.runtime_profile,
                    compatibility=skill.compatibility,
                )
            )
    return result


def list_managed_skills(
    root: Path,
    *,
    max_files: int,
    max_bytes: int,
    builtin_root: Path | None = BUILTIN_SKILLS_ROOT,
    project_roots: tuple[Path, ...] = (),
) -> list[ManagedSkill]:
    """三层都列出来，出厂、用户、项目依次排列。

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
    disabled = disabled_skill_names(resolved)
    project: list[ManagedSkill] = []
    project_names: set[str] = set()
    for workspace_root in project_roots:
        root_items = _scan_managed(
            _safe_root(workspace_root / ".workpilot" / "skills"),
            origin="project",
            max_files=max_files,
            max_bytes=max_bytes,
            enabled_for=lambda _: True,
        )
        for item in root_items:
            loadable = item.error is None and item.enabled and item.name not in disabled
            duplicate = loadable and item.name in project_names
            project.append(
                replace(
                    item,
                    enabled=loadable,
                    shadowed=duplicate,
                )
            )
            if loadable and not duplicate:
                project_names.add(item.name)
    if builtin_root is None:
        return [replace(item, shadowed=item.name in project_names) for item in user] + project
    user_names = {
        item.name
        for item in user
        if item.error is None and item.enabled and item.name not in disabled
    }
    user = [
        replace(
            item,
            enabled=item.name not in disabled,
            shadowed=item.name in project_names,
        )
        for item in user
    ]
    builtin = [
        replace(item, shadowed=item.name in user_names or item.name in project_names)
        for item in _scan_managed(
            _safe_root(builtin_root),
            origin="builtin",
            max_files=max_files,
            max_bytes=max_bytes,
            enabled_for=lambda child: child.name not in disabled,
        )
    ]
    project = [
        replace(item, enabled=item.enabled and item.name not in disabled)
        for item in project
    ]
    return builtin + user + project


def install_skill(
    root: Path,
    *,
    name: str,
    skill_md: str,
    enabled: bool,
    max_bytes: int,
    replace: bool,
    _internal_files: dict[str, str] | None = None,
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
        for internal_name, internal_body in (_internal_files or {}).items():
            if internal_name not in _INTERNAL_SKILL_FILES:
                raise SkillCatalogError("Skill 内部元数据文件名无效")
            _write_private_file(final_staging / internal_name, internal_body)
        if target.is_dir() and not target.is_symlink():
            for source in target.iterdir():
                if (
                    source.name in {"SKILL.md", ".disabled", *_INTERNAL_SKILL_FILES}
                    or source.is_symlink()
                ):
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
    provenance_signing_key: str,
    builtin_root: Path | None = BUILTIN_SKILLS_ROOT,
) -> ManagedSkill:
    """凭签名来源收据幂等更新自动 Skill；绝不靠正文子串判断来源。"""

    if not provenance_signing_key:
        raise SkillCatalogError("自动 Skill 来源签名键不可用")
    privacy_reason = skill_persistence_skip_reason(skill_md)
    if privacy_reason is not None:
        raise SkillCatalogError("自动 Skill 包含禁止持久化的敏感信息")

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
        if not _valid_auto_distilled_receipt(
            target,
            name=name,
            capability_key=capability_key,
            provenance_signing_key=provenance_signing_key,
            max_bytes=max_bytes,
        ):
            raise FileExistsError(f"同名人工 Skill 已存在，自动晋升未覆盖: {name}")
    receipt = _build_auto_distilled_receipt(
        name=name,
        capability_key=capability_key,
        skill_md=skill_md,
        provenance_signing_key=provenance_signing_key,
    )
    return install_skill(
        resolved,
        name=name,
        skill_md=skill_md,
        enabled=True,
        max_bytes=max_bytes,
        replace=replace,
        _internal_files={_AUTO_DISTILLED_RECEIPT: receipt},
    )


def _auto_receipt_payload(*, name: str, capability_key: str, skill_sha256: str) -> dict[str, Any]:
    return {
        "version": 1,
        "origin": "auto_distilled",
        "name": name,
        "capability_key": capability_key,
        "skill_sha256": skill_sha256,
    }


def _receipt_signature(payload: dict[str, Any], provenance_signing_key: str) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(provenance_signing_key.encode("utf-8"), encoded, hashlib.sha256).hexdigest()


def _build_auto_distilled_receipt(
    *,
    name: str,
    capability_key: str,
    skill_md: str,
    provenance_signing_key: str,
) -> str:
    payload = _auto_receipt_payload(
        name=name,
        capability_key=capability_key,
        skill_sha256=hashlib.sha256(skill_md.encode("utf-8")).hexdigest(),
    )
    return json.dumps(
        {**payload, "signature": _receipt_signature(payload, provenance_signing_key)},
        sort_keys=True,
        separators=(",", ":"),
    )


def _valid_auto_distilled_receipt(
    target: Path,
    *,
    name: str,
    capability_key: str,
    provenance_signing_key: str,
    max_bytes: int,
) -> bool:
    marker = target / _AUTO_DISTILLED_RECEIPT
    skill_file = target / "SKILL.md"
    if (
        target.is_symlink()
        or marker.is_symlink()
        or skill_file.is_symlink()
        or not marker.is_file()
        or not skill_file.is_file()
    ):
        return False
    try:
        marker_raw = marker.read_bytes()
        skill_raw = skill_file.read_bytes()
        if (
            not marker_raw
            or len(marker_raw) > _AUTO_RECEIPT_MAX_BYTES
            or not skill_raw
            or len(skill_raw) > max_bytes
        ):
            return False
        receipt = json.loads(marker_raw)
    except (OSError, UnicodeError, ValueError):
        return False
    if not isinstance(receipt, dict) or set(receipt) != {
        "version",
        "origin",
        "name",
        "capability_key",
        "skill_sha256",
        "signature",
    }:
        return False
    payload = _auto_receipt_payload(
        name=name,
        capability_key=capability_key,
        skill_sha256=hashlib.sha256(skill_raw).hexdigest(),
    )
    if any(receipt.get(key) != value for key, value in payload.items()):
        return False
    signature = receipt.get("signature")
    return isinstance(signature, str) and hmac.compare_digest(
        signature,
        _receipt_signature(payload, provenance_signing_key),
    )


def _write_private_file(path: Path, body: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(body)


def _write_marker(marker: Path, body: str) -> None:
    if marker.parent.is_symlink():
        raise SkillCatalogError("Skill 停用标记目录不能是符号链接")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(marker, flags, 0o600)
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
    """设置名称级开关；关闭后任何 origin 的同名 Skill 都不能 fallback 复活。"""

    resolved = _safe_root(root)
    target = _skill_dir(resolved, name)
    selected: tuple[Path, SkillOrigin] | None = None
    if target.is_dir() and not target.is_symlink():
        selected = (target, "user")
    elif builtin_root is not None:
        builtin_dir = _skill_dir(_safe_root(builtin_root), name)
        if builtin_dir.is_dir() and not builtin_dir.is_symlink():
            selected = (builtin_dir, "builtin")
    if selected is None:
        raise FileNotFoundError(f"Skill 不存在: {name}")

    marker_root = resolved / BUILTIN_DISABLED_DIRNAME
    if marker_root.exists() and (not marker_root.is_dir() or marker_root.is_symlink()):
        raise SkillCatalogError("Skill 停用标记目录必须是普通目录")
    marker_root.mkdir(parents=True, exist_ok=True)
    marker = marker_root / name
    legacy_marker = target / ".disabled" if target.is_dir() and not target.is_symlink() else None
    if enabled:
        marker.unlink(missing_ok=True)
        if legacy_marker is not None:
            legacy_marker.unlink(missing_ok=True)
    else:
        _write_marker(marker, "disabled\n")
        if legacy_marker is not None:
            legacy_marker.unlink(missing_ok=True)
    result = _managed_one(selected[0], max_bytes=max_bytes, origin=selected[1])
    return replace(result, enabled=enabled)


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
            if (
                not source.is_file()
                or source.name in {"SKILL.md", *_INTERNAL_SKILL_FILES}
                or source.is_symlink()
            ):
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
    if not skill_dir.is_dir() or skill_dir.is_symlink():
        raise FileNotFoundError(name)
    return read_skill_resource_file(
        skill_dir / "SKILL.md",
        resource=resource,
        max_bytes=max_bytes,
    )


def _managed_one(target: Path, *, max_bytes: int, origin: SkillOrigin = "user") -> ManagedSkill:
    skill = load_skill_file(target / "SKILL.md", max_bytes=max_bytes, origin=origin)
    resources = tuple(
        path.relative_to(target).as_posix()
        for path in sorted(target.rglob("*"))
        if path.is_file()
        and path.name not in {"SKILL.md", ".disabled", *_INTERNAL_SKILL_FILES}
        and not path.is_symlink()
    )
    return ManagedSkill(
        name=skill.name,
        enabled=not (target / ".disabled").exists(),
        description=skill.description,
        sha256=skill.sha256,
        resources=resources,
        error=None,
        origin=origin,
        kind=skill.kind,
        runtime_profile=skill.runtime_profile,
        compatibility=skill.compatibility,
    )


def _safe_root(root: Path) -> Path:
    expanded = root.expanduser()
    if expanded.exists() and (not expanded.is_dir() or expanded.is_symlink()):
        raise SkillCatalogError("Skill 根目录必须是普通目录")
    return expanded.resolve()


def _skill_directory_from_file(skill_file: Path) -> Path:
    if skill_file.name != "SKILL.md" or skill_file.is_symlink() or skill_file.parent.is_symlink():
        raise SkillCatalogError("Skill resource 必须绑定普通 SKILL.md")
    try:
        resolved_file = skill_file.resolve(strict=True)
        resolved_dir = skill_file.parent.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise FileNotFoundError(skill_file.name) from error
    if (
        not resolved_file.is_file()
        or resolved_file.parent != resolved_dir
        or resolved_dir.is_symlink()
    ):
        raise SkillCatalogError("Skill resource 目录非法")
    return resolved_dir


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
