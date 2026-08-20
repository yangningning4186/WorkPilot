import json
from pathlib import Path

import pytest

from app.cowork.skills.distillation import SkillDistillationError, parse_distilled_skill
from app.cowork.skills.lifecycle import install_auto_distilled_skill, install_skill


def _candidate(**updates: object) -> str:
    value: dict[str, object] = {
        "capability_key": "summarize-office-report",
        "name": "summarize-office-report",
        "description": "将办公文档整理为结构化摘要",
        "triggers": ["总结一份办公文档"],
        "anti_triggers": ["只翻译原文"],
        "tools": ["read_text_file", "create_artifact"],
        "steps": ["读取用户指定的源文档", "按主题归纳并创建摘要交付物"],
        "confidence": 0.91,
    }
    value.update(updates)
    return json.dumps({"candidate": value}, ensure_ascii=False)


def test_distilled_skill_is_built_from_structured_fields_only() -> None:
    result = parse_distilled_skill(
        _candidate(), successful_tools={"read_text_file", "create_artifact"}
    )

    assert result is not None
    assert result.name == "learned-summarize-office-report"
    assert "origin: auto_distilled" in result.skill_md
    assert "1. 读取用户指定的源文档" in result.skill_md


def test_distilled_skill_rejects_unobserved_or_high_risk_tools() -> None:
    with pytest.raises(SkillDistillationError, match="未使用"):
        parse_distilled_skill(
            _candidate(tools=["read_text_file", "write_text_file"]),
            successful_tools={"read_text_file"},
        )
    with pytest.raises(SkillDistillationError, match="禁止自动晋升"):
        parse_distilled_skill(
            _candidate(tools=["run_shell"]), successful_tools={"run_shell"}
        )


def test_auto_distilled_install_never_overwrites_manual_skill(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    manual = _candidate()
    parsed = parse_distilled_skill(
        manual, successful_tools={"read_text_file", "create_artifact"}
    )
    assert parsed is not None
    manual_md = parsed.skill_md.replace("origin: auto_distilled", "origin: manual")
    install_skill(
        root,
        name=parsed.name,
        skill_md=manual_md,
        enabled=True,
        max_bytes=64_000,
        replace=False,
    )

    with pytest.raises(FileExistsError, match="人工 Skill"):
        install_auto_distilled_skill(
            root,
            name=parsed.name,
            capability_key=parsed.capability_key,
            skill_md=parsed.skill_md,
            max_bytes=64_000,
        )
