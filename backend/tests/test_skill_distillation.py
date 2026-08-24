import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from uuid6 import uuid7

from app.core.config import Settings
from app.cowork.skills.distillation import SkillDistillationError, parse_distilled_skill
from app.cowork.skills.lifecycle import install_auto_distilled_skill, install_skill
from app.worker import skill_distillation_run


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
        parse_distilled_skill(_candidate(tools=["run_shell"]), successful_tools={"run_shell"})


def test_auto_distilled_install_never_overwrites_manual_skill(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    manual = _candidate()
    parsed = parse_distilled_skill(manual, successful_tools={"read_text_file", "create_artifact"})
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


async def test_distillation_worker_reuses_the_source_conversation_provider(
    tmp_path: Path, monkeypatch
) -> None:
    run_id = uuid7()
    conversation_id = uuid7()
    source = SimpleNamespace(goal="整理报告", final_message="已完成", successful_tools=[])
    monkeypatch.setattr(skill_distillation_run, "claim_skill_job", lambda *args, **kwargs: source)
    monkeypatch.setattr(skill_distillation_run, "complete_skill_job", lambda *args, **kwargs: True)
    store = SimpleNamespace(
        get_run=AsyncMock(return_value=SimpleNamespace(conversation_id=conversation_id))
    )
    monkeypatch.setattr(skill_distillation_run, "cowork_store", lambda: store)
    gateway = AsyncMock()
    build_gateway = AsyncMock(return_value=gateway)
    monkeypatch.setattr(skill_distillation_run, "build_conversation_gateway", build_gateway)
    monkeypatch.setattr(
        skill_distillation_run,
        "distill_skill_candidate",
        AsyncMock(return_value=None),
    )

    @asynccontextmanager
    async def fake_session_factory():
        yield object()

    await skill_distillation_run.skill_distillation_job(
        {
            "settings": Settings(
                skill_distillation_enabled=True,
                cowork_skill_candidates_path=tmp_path / "candidates",
            ),
            "session_factory": fake_session_factory,
        },
        str(run_id),
    )

    assert build_gateway.await_args.kwargs["conversation_id"] == conversation_id
    gateway.aclose.assert_awaited_once()
