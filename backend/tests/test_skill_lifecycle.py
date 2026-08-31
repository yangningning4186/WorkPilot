import base64
import io
import zipfile
from pathlib import Path
from typing import BinaryIO

import pytest

from app.cowork.skills.catalog import SkillCatalogError, load_skill_catalog
from app.cowork.skills.lifecycle import (
    import_skill_zip,
    install_skill,
    list_managed_skills,
    list_skill_resources,
    read_skill_resource,
    set_skill_enabled,
)

SKILL_MD = """---
name: summarize
description: Summarize a local document
trigger:
  - summarize a document
anti_trigger:
  - translate only
tools:
  - read_text_file
---
Read the source, identify the audience, then write a concise summary.
"""


def _user_names(root: Path) -> list[str]:
    catalog = load_skill_catalog(root, max_files=20, max_bytes=64_000, builtin_root=None)
    return [skill.name for skill in catalog.skills]


def test_skill_install_disable_enable_and_resource(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    installed = install_skill(
        root,
        name="summarize",
        skill_md=SKILL_MD,
        enabled=True,
        max_bytes=64_000,
        replace=False,
    )

    assert installed.enabled is True
    # builtin_root=None：这条用例查的是 user 层自己的生命周期，出厂层会一直在场，
    # 混进来只会让每个断言都要先过滤一次。合并行为由 test_cowork_skills.py 覆盖。
    assert _user_names(root) == ["summarize"]

    (root / "summarize" / "references").mkdir()
    (root / "summarize" / "references" / "style.md").write_text("Keep it short.", encoding="utf-8")
    disabled = set_skill_enabled(root, name="summarize", enabled=False, max_bytes=64_000)
    assert disabled.enabled is False
    assert _user_names(root) == []

    install_skill(
        root,
        name="summarize",
        skill_md=SKILL_MD.replace("concise", "auditable"),
        enabled=True,
        max_bytes=64_000,
        replace=True,
    )
    content, resource = read_skill_resource(
        root,
        name="summarize",
        resource="references/style.md",
        max_bytes=64_000,
    )
    assert (content, resource) == ("Keep it short.", "references/style.md")
    assert list_managed_skills(root, max_files=20, max_bytes=64_000)[0].enabled is True


def test_skill_install_validates_name_matches_directory(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="所在目录同名"):
        install_skill(
            tmp_path / "skills",
            name="different",
            skill_md=SKILL_MD,
            enabled=True,
            max_bytes=64_000,
            replace=False,
        )


def test_skill_resource_rejects_symlinked_parent_escape(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    install_skill(
        root,
        name="summarize",
        skill_md=SKILL_MD,
        enabled=True,
        max_bytes=64_000,
        replace=False,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("host secret", encoding="utf-8")
    (root / "summarize" / "references").symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileNotFoundError):
        read_skill_resource(
            root,
            name="summarize",
            resource="references/secret.txt",
            max_bytes=64_000,
        )


def test_skill_resource_enumeration_bounds_directory_entries(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    install_skill(
        root,
        name="summarize",
        skill_md=SKILL_MD,
        enabled=True,
        max_bytes=64_000,
        replace=False,
    )
    references = root / "summarize" / "references"
    references.mkdir()
    (references / "one.md").write_text("one", encoding="utf-8")

    with pytest.raises(SkillCatalogError, match="目录项超过扫描上限"):
        list_skill_resources(
            root / "summarize" / "SKILL.md",
            max_files=2,
            max_bytes=64_000,
        )


def test_skill_resource_enumeration_ignores_build_and_dependency_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    install_skill(
        root,
        name="summarize",
        skill_md=SKILL_MD,
        enabled=True,
        max_bytes=64_000,
        replace=False,
    )
    skill = root / "summarize"
    (skill / "scripts").mkdir()
    (skill / "scripts" / "tool.js").write_text("export {};", encoding="utf-8")
    for ignored in ("node_modules", "dist", "__pycache__"):
        directory = skill / ignored / "nested"
        directory.mkdir(parents=True)
        (directory / "ignored.txt").write_text("ignored", encoding="utf-8")

    resources = list_skill_resources(
        skill / "SKILL.md",
        max_files=10,
        max_bytes=64_000,
    )
    managed = list_managed_skills(
        root,
        max_files=20,
        max_bytes=64_000,
        builtin_root=None,
    )

    assert [item.path for item in resources] == ["scripts/tool.js"]
    assert managed[0].resources == ("scripts/tool.js",)


def test_broken_project_skill_does_not_shadow_loadable_user_skill(tmp_path: Path) -> None:
    user_root = tmp_path / "user-skills"
    install_skill(
        user_root,
        name="summarize",
        skill_md=SKILL_MD,
        enabled=True,
        max_bytes=64_000,
        replace=False,
    )
    workspace = tmp_path / "workspace"
    broken = workspace / ".workpilot" / "skills" / "summarize"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("---\nname: summarize\n---\n", encoding="utf-8")

    managed = list_managed_skills(
        user_root,
        max_files=20,
        max_bytes=64_000,
        builtin_root=None,
        project_roots=(workspace,),
    )

    user = next(item for item in managed if item.origin == "user")
    project = next(item for item in managed if item.origin == "project")
    assert user.shadowed is False
    assert user.enabled is True
    assert project.error is not None
    assert project.enabled is False


def test_skill_zip_enforces_actual_streamed_member_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("summarize/SKILL.md", SKILL_MD)
        bundle.writestr("summarize/references/bomb.bin", b"x")
    original_open = zipfile.ZipFile.open

    def oversized_open(
        bundle: zipfile.ZipFile,
        member: str | zipfile.ZipInfo,
        mode: str = "r",
        pwd: bytes | None = None,
        *,
        force_zip64: bool = False,
    ) -> BinaryIO:
        name = member.filename if isinstance(member, zipfile.ZipInfo) else member
        if name == "summarize/references/bomb.bin":
            return io.BytesIO(b"x" * 1_025)
        return original_open(bundle, member, mode, pwd, force_zip64=force_zip64)

    monkeypatch.setattr(zipfile.ZipFile, "open", oversized_open)

    with pytest.raises(SkillCatalogError, match="实际解压超过大小上限"):
        import_skill_zip(
            tmp_path / "skills",
            archive_base64=base64.b64encode(archive_buffer.getvalue()).decode("ascii"),
            enabled=True,
            max_bytes=1_024,
        )

    assert not (tmp_path / "skills" / "summarize").exists()


def test_skill_zip_imports_bounded_resource_streams(tmp_path: Path) -> None:
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("summarize/SKILL.md", SKILL_MD)
        bundle.writestr("summarize/references/style.md", "Keep it short.")

    imported = import_skill_zip(
        tmp_path / "skills",
        archive_base64=base64.b64encode(archive_buffer.getvalue()).decode("ascii"),
        enabled=True,
        max_bytes=1_024,
    )

    assert imported.name == "summarize"
    assert read_skill_resource(
        tmp_path / "skills",
        name="summarize",
        resource="references/style.md",
        max_bytes=1_024,
    ) == ("Keep it short.", "references/style.md")
