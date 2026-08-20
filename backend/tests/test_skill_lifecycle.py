from pathlib import Path

import pytest

from app.cowork.skills.catalog import load_skill_catalog
from app.cowork.skills.lifecycle import (
    install_skill,
    list_managed_skills,
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
    assert [skill.name for skill in load_skill_catalog(root, max_files=20, max_bytes=64_000).skills] == [
        "summarize"
    ]

    (root / "summarize" / "references").mkdir()
    (root / "summarize" / "references" / "style.md").write_text("Keep it short.", encoding="utf-8")
    disabled = set_skill_enabled(root, name="summarize", enabled=False, max_bytes=64_000)
    assert disabled.enabled is False
    assert load_skill_catalog(root, max_files=20, max_bytes=64_000).skills == ()

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
