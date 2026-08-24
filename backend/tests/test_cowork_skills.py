from pathlib import Path

import pytest

from app.core.config import Settings
from app.cowork.extensions import register_skill_tools
from app.cowork.skills.catalog import (
    BUILTIN_DISABLED_DIRNAME,
    BUILTIN_SKILLS_ROOT,
    SkillCatalogError,
    load_skill_catalog,
)
from app.cowork.skills.lifecycle import (
    install_auto_distilled_skill,
    list_managed_skills,
    read_skill_definition_resource,
    remove_skill,
    set_skill_enabled,
)
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

    # 出厂层仍在，所以断言的是"这条 user 技能没进目录"，不是"目录是空的"。
    assert [skill.name for skill in catalog.skills if skill.origin == "user"] == []
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
    assert registry.runtime_snapshot()["skills"]["snapshot_sha256"] == (catalog.snapshot_sha256)
    ordinary_goal_tools = {tool.name for tool in registry.tool_definitions_for("整理当前工作目录")}
    assert {"run_shell", "list_skills", "load_skill", "load_skill_resource"} <= (
        ordinary_goal_tools
    )
    assert registry.requires_approval("model_hallucinated_tool") is False


# --- 出厂 Skill 层 -------------------------------------------------------------


def _write_named_skill(root: Path, name: str, description: str) -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"""---
name: {name}
description: {description}
trigger: [随便]
tools: [read_text_file]
status: active
---

一步。
""",
        encoding="utf-8",
    )


def test_builtin_skills_ship_with_the_product(tmp_path: Path) -> None:
    """装完就该有技能可用——空的 user 目录也不能给出空目录。"""

    catalog = load_skill_catalog(tmp_path, max_files=20, max_bytes=20_000)

    names = {skill.name for skill in catalog.skills}
    assert {
        "skill-creator",
        "immersive-reading",
        "docx",
        "xlsx",
        "pptx",
        "pdf",
    } <= names
    assert all(skill.origin == "builtin" for skill in catalog.skills)
    assert catalog.errors == ()
    # 出厂正文自己必须过同一套校验，否则发出去才发现装不上。
    assert "anti_trigger" in catalog.get("skill-creator").procedure
    assert "run_shell" in catalog.get("docx").tools


def test_user_skill_shadows_builtin_of_the_same_name(tmp_path: Path) -> None:
    _write_named_skill(tmp_path, "skill-creator", "我自己那份")

    catalog = load_skill_catalog(tmp_path, max_files=20, max_bytes=20_000)

    forked = catalog.get("skill-creator")
    assert forked.origin == "user"
    assert forked.description == "我自己那份"
    # 只剩一条，不会两条同名一起进 prompt。
    assert [skill.name for skill in catalog.skills].count("skill-creator") == 1
    # 但"被盖住了"必须看得见。
    assert catalog.shadowed == ("skill-creator",)


def test_disabling_a_builtin_writes_the_marker_into_the_user_layer(tmp_path: Path) -> None:
    """停用标记不能落在出厂目录里：那里下次升级会被整个替换掉。"""

    set_skill_enabled(tmp_path, name="skill-creator", enabled=False, max_bytes=20_000)

    assert (tmp_path / BUILTIN_DISABLED_DIRNAME / "skill-creator").is_file()
    assert not (BUILTIN_SKILLS_ROOT / "skill-creator" / ".disabled").exists()
    catalog = load_skill_catalog(tmp_path, max_files=20, max_bytes=20_000)
    assert "skill-creator" not in {skill.name for skill in catalog.skills}

    set_skill_enabled(tmp_path, name="skill-creator", enabled=True, max_bytes=20_000)
    catalog = load_skill_catalog(tmp_path, max_files=20, max_bytes=20_000)
    assert "skill-creator" in {skill.name for skill in catalog.skills}


def test_enabling_toggles_the_fork_not_the_builtin_when_both_exist(tmp_path: Path) -> None:
    """有 fork 时开关必须管 fork——它才是实际生效的那份。"""

    _write_named_skill(tmp_path, "skill-creator", "我自己那份")

    result = set_skill_enabled(tmp_path, name="skill-creator", enabled=False, max_bytes=20_000)

    assert result.origin == "user"
    assert (tmp_path / "skill-creator" / ".disabled").is_file()
    assert not (tmp_path / BUILTIN_DISABLED_DIRNAME / "skill-creator").exists()
    # fork 停用之后出厂那份重新露出来，而不是变成"什么都没有"。
    catalog = load_skill_catalog(tmp_path, max_files=20, max_bytes=20_000)
    assert catalog.get("skill-creator").origin == "builtin"


def test_builtin_cannot_be_removed_and_the_error_names_the_two_ways_out(tmp_path: Path) -> None:
    with pytest.raises(SkillCatalogError) as excinfo:
        remove_skill(tmp_path, name="skill-creator")

    message = str(excinfo.value)
    assert "出厂" in message and "enabled" in message and "覆盖" in message


def test_removing_a_fork_restores_the_builtin(tmp_path: Path) -> None:
    _write_named_skill(tmp_path, "skill-creator", "我自己那份")

    remove_skill(tmp_path, name="skill-creator")

    catalog = load_skill_catalog(tmp_path, max_files=20, max_bytes=20_000)
    assert catalog.get("skill-creator").origin == "builtin"
    assert catalog.shadowed == ()


def test_auto_distillation_never_shadows_a_builtin(tmp_path: Path) -> None:
    """自动晋升拿走出厂名字 = 用一段没人审过的正文换掉出厂流程，而列表里看不出来。"""

    with pytest.raises(FileExistsError, match="出厂"):
        install_auto_distilled_skill(
            tmp_path,
            name="skill-creator",
            capability_key="skill-creator",
            skill_md="---\nname: skill-creator\ndescription: x\nstatus: active\n---\n\n一步。\n",
            max_bytes=20_000,
        )


def test_list_managed_skills_keeps_the_shadowed_builtin_visible(tmp_path: Path) -> None:
    _write_named_skill(tmp_path, "skill-creator", "我自己那份")

    items = list_managed_skills(tmp_path, max_files=50, max_bytes=20_000)

    by_origin = {(item.name, item.origin): item for item in items}
    assert by_origin[("skill-creator", "builtin")].shadowed is True
    assert by_origin[("skill-creator", "builtin")].public()["removable"] is False
    assert by_origin[("skill-creator", "user")].public()["removable"] is True


def test_project_skill_overrides_user_and_builtin_and_reads_its_own_resource(
    tmp_path: Path,
) -> None:
    user_root = tmp_path / "user"
    project_root = tmp_path / "project"
    _write_named_skill(user_root, "skill-creator", "用户层")
    project_skill = project_root / ".workpilot" / "skills" / "skill-creator"
    _write_named_skill(project_skill.parent, "skill-creator", "项目层")
    (project_skill / "guide.txt").write_text("项目资源", encoding="utf-8")

    catalog = load_skill_catalog(
        user_root,
        max_files=20,
        max_bytes=20_000,
        project_roots=(project_root,),
    )

    selected = catalog.get("skill-creator")
    assert selected.origin == "project"
    assert selected.description == "项目层"
    assert catalog.shadowed == ("skill-creator",)
    assert read_skill_definition_resource(
        selected.source_path, resource="guide.txt", max_bytes=20_000
    ) == ("项目资源", "guide.txt")
