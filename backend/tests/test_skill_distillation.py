import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from uuid6 import uuid7

from app.core.config import Settings
from app.cowork.connector_tools import register_feishu_tools
from app.cowork.memory_tools import register_memory_tools
from app.cowork.skills.candidate_store import get_skill_candidate
from app.cowork.skills.distillation import (
    SkillDistillationError,
    parse_distilled_skill,
    promotion_review_required_tools,
)
from app.cowork.skills.lifecycle import install_auto_distilled_skill, install_skill
from app.cowork.tools import build_default_cowork_registry
from app.worker import skill_distillation_run
from app.worker.skill_distillation_run import _record_and_gate


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


def test_distilled_skill_rejects_personal_or_secret_content() -> None:
    with pytest.raises(SkillDistillationError, match="敏感信息"):
        parse_distilled_skill(
            _candidate(steps=["把结果发送给 alice@example.com"]),
            successful_tools={"read_text_file", "create_artifact"},
        )
    with pytest.raises(SkillDistillationError, match=r"敏感信息|凭据"):
        parse_distilled_skill(
            _candidate(steps=["使用 access_token=top-secret-value 调用接口"]),
            successful_tools={"read_text_file", "create_artifact"},
        )


def test_auto_promotion_risk_uses_registered_effect_and_capability() -> None:
    registry = build_default_cowork_registry()
    register_memory_tools(registry)
    register_feishu_tools(registry)

    tools = [
        "read_file",
        "write_file",
        "request_capability",
        "remember",
        "feishu_calendar_event_action",
        "feishu_base_record_action",
        "feishu_approval_submit",
        "unknown_future_tool",
    ]
    review = promotion_review_required_tools(registry, tools)

    assert "read_file" not in review
    assert {
        "write_file",  # effect=filesystem
        "request_capability",  # interaction，可产生持久 authority
        "remember",  # effect 元数据不足以证明是只读控制面
        "feishu_calendar_event_action",  # 动态 external write/delete
        "feishu_base_record_action",  # 动态 external write/delete
        "feishu_approval_submit",  # external.write + approval
        "unknown_future_tool",  # 未知工具 fail closed
    } <= set(review)


def test_risky_distilled_skill_stops_at_needs_review(tmp_path: Path) -> None:
    parsed = parse_distilled_skill(
        _candidate(tools=["feishu_calendar_event_action"]),
        successful_tools={"feishu_calendar_event_action"},
    )
    assert parsed is not None
    settings = Settings(
        cowork_skill_candidates_path=tmp_path / "candidates",
        cowork_skills_path=tmp_path / "skills",
        skill_auto_promotion_enabled=True,
        skill_promotion_min_evidence=2,
    )

    _record_and_gate(
        settings,
        uuid7(),
        parsed,
        review_required_tools=("feishu_calendar_event_action",),
    )

    candidate = get_skill_candidate(settings.cowork_skill_candidates_path, parsed.capability_key)
    assert candidate is not None
    assert candidate.status == "needs_review"
    assert "feishu_calendar_event_action" in str(candidate.review_reason)
    assert not (settings.cowork_skills_path / parsed.name).exists()


def test_proven_read_only_distilled_skill_can_still_auto_promote(tmp_path: Path) -> None:
    parsed = parse_distilled_skill(_candidate(tools=["read_file"]), successful_tools={"read_file"})
    assert parsed is not None
    settings = Settings(
        cowork_skill_candidates_path=tmp_path / "candidates",
        cowork_skills_path=tmp_path / "skills",
        skill_auto_promotion_enabled=True,
        skill_promotion_min_evidence=2,
    )

    for _ in range(2):
        _record_and_gate(
            settings,
            uuid7(),
            parsed,
            review_required_tools=(),
        )

    candidate = get_skill_candidate(settings.cowork_skill_candidates_path, parsed.capability_key)
    assert candidate is not None and candidate.status == "promoted"
    assert (settings.cowork_skills_path / parsed.name / "SKILL.md").is_file()


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
            provenance_signing_key="test-provenance-key",
        )


def test_auto_distilled_update_requires_signed_receipt_not_body_markers(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    parsed = parse_distilled_skill(
        _candidate(), successful_tools={"read_text_file", "create_artifact"}
    )
    assert parsed is not None
    forged = (
        parsed.skill_md + "\n# origin: auto_distilled\n# capability_key: summarize-office-report\n"
    )
    install_skill(
        root,
        name=parsed.name,
        skill_md=forged,
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
            provenance_signing_key="test-provenance-key",
        )


def test_auto_distilled_receipt_is_bound_to_exact_skill_content(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    parsed = parse_distilled_skill(
        _candidate(), successful_tools={"read_text_file", "create_artifact"}
    )
    assert parsed is not None
    key = "test-provenance-key"
    install_auto_distilled_skill(
        root,
        name=parsed.name,
        capability_key=parsed.capability_key,
        skill_md=parsed.skill_md,
        max_bytes=64_000,
        provenance_signing_key=key,
    )
    skill_path = root / parsed.name / "SKILL.md"
    skill_path.write_text(parsed.skill_md + "\n人工改动\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="人工 Skill"):
        install_auto_distilled_skill(
            root,
            name=parsed.name,
            capability_key=parsed.capability_key,
            skill_md=parsed.skill_md,
            max_bytes=64_000,
            provenance_signing_key=key,
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
