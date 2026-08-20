from pathlib import Path

import pytest

from app.core.config import Settings
from app.cowork.extensions import register_skill_tools
from app.cowork.skills.catalog import SkillCatalogError, load_skill_catalog
from app.cowork.tools import build_default_cowork_registry


def _write_skill(root: Path, name: str = "contract-review") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    path = directory / "SKILL.md"
    path.write_text(
        """---
name: contract-review
description: 提取合同关键条款并标记缺失项
trigger:
  - 用户要求审阅合同
anti_trigger:
  - 用户要求正式法律意见
tools: [read_text_file]
status: active
---

1. 读取合同原文。
2. 提取主体、期限、金额与违约责任。
""",
        encoding="utf-8",
    )
    return path


def test_load_skill_catalog_builds_stable_snapshot(tmp_path: Path) -> None:
    _write_skill(tmp_path)

    first = load_skill_catalog(tmp_path, max_files=20, max_bytes=20_000)
    second = load_skill_catalog(tmp_path, max_files=20, max_bytes=20_000)

    assert first.snapshot_sha256 == second.snapshot_sha256
    assert first.get("contract-review").trigger == ("用户要求审阅合同",)
    assert first.get("contract-review").anti_trigger == ("用户要求正式法律意见",)
    assert "提取主体" in first.get("contract-review").procedure


def test_skill_name_must_match_directory(tmp_path: Path) -> None:
    _write_skill(tmp_path, name="wrong-directory")

    catalog = load_skill_catalog(tmp_path, max_files=20, max_bytes=20_000)

    assert catalog.skills == ()
    assert "必须与所在目录同名" in catalog.errors[0]
    with pytest.raises(SkillCatalogError, match="未知或未启用"):
        catalog.get("contract-review")


def test_register_skill_tools_adds_catalog_and_runtime_snapshot(tmp_path: Path) -> None:
    _write_skill(tmp_path)
    registry = build_default_cowork_registry()
    settings = Settings(cowork_skills_path=tmp_path)

    catalog = register_skill_tools(registry, settings)

    assert registry.get("list_skills").risk == "read"
    assert registry.get("load_skill").effect == "none"
    assert "contract-review" in registry.system_instructions()
    assert registry.runtime_snapshot()["skills"]["snapshot_sha256"] == (
        catalog.snapshot_sha256
    )
    ordinary_goal_tools = {
        tool.name for tool in registry.tool_definitions_for("整理当前工作目录")
    }
    assert {"run_shell", "list_skills", "load_skill", "load_skill_resource"} <= (
        ordinary_goal_tools
    )
    assert registry.requires_approval("model_hallucinated_tool") is False
