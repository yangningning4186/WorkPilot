from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from app.core.config import Settings
from app.core.db import DbSession as AsyncSession
from app.cowork.capabilities import (
    CapabilityActivation,
    CapabilityPreLoopContext,
    WorkCapability,
    WorkCapabilityRegistry,
    build_work_capability_registry,
)
from app.cowork.personas import approval_mode_for_persona_change, load_persona_catalog
from app.cowork.runtime import CoworkState, _scoped_allowed_tools
from app.cowork.tools import build_default_cowork_registry
from app.runstore.conversations import (
    get_conversation,
    update_conversation_runtime,
)
from app.runstore.runs import ensure_conversation


async def test_reading_capability_owns_prompt_tools_and_pre_loop() -> None:
    calls: list[str] = []

    async def pre_loop(context: CapabilityPreLoopContext) -> dict[str, str]:
        calls.append(str(context.state["goal"]))
        return {"locate_block": "命中第 12 页"}

    registry = build_work_capability_registry(reading_pre_loop=pre_loop)
    activation = CapabilityActivation(
        goal="解释方法",
        work_mode="reading",
        reading_path="paper.pdf",
    )
    resolved = registry.resolve(activation)

    assert resolved.names == ("reading",)
    assert {"material_outline", "read_material", "reader_goto"} <= resolved.owned_tools
    assert "<reading_mode>" in resolved.render_system_block(activation)
    assert await resolved.run_pre_loop(
        CapabilityPreLoopContext(state={"goal": "解释方法"}, services={})
    ) == {"locate_block": "命中第 12 页"}
    assert calls == ["解释方法"]


def test_office_capability_mounts_hot_path_tools_not_admin_routes() -> None:
    resolved = build_work_capability_registry().resolve(
        CapabilityActivation(goal="整理季度报告", work_mode="office")
    )

    assert {"list_files", "read_file", "write_file", "load_skill", "run_shell"} <= (
        resolved.owned_tools
    )
    assert {"run_sandbox", "list_skills"}.isdisjoint(resolved.owned_tools)


def test_exclusive_capability_is_a_tool_surface_rule_not_a_permission_grant() -> None:
    exclusive = WorkCapability(
        name="deep-research", owned_tools=frozenset({"web_search"}), exclusive=True
    )
    additive = WorkCapability(name="format", owned_tools=frozenset({"run_shell"}))
    resolved = WorkCapabilityRegistry((additive, exclusive)).resolve(
        CapabilityActivation(goal="研究", work_mode="office")
    )

    assert resolved.names == ("deep-research",)
    assert resolved.owned_tools == frozenset({"web_search"})
    with pytest.raises(ValueError, match="多个 exclusive"):
        WorkCapabilityRegistry((exclusive, WorkCapability(name="other", exclusive=True))).resolve(
            CapabilityActivation(goal="研究", work_mode="office")
        )


def test_project_persona_overrides_builtin_and_restricts_tool_surface(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    persona_root = project / ".workpilot" / "personas"
    persona_root.mkdir(parents=True)
    (persona_root / "researcher.toml").write_text(
        """name = "researcher"
label = "项目研究员"
description = "只允许读取项目资料"
tool_patterns = ["read_*", "search_*"]
default_approval_mode = "interactive"
recommended_connectors = ["feishu"]
recommended_work_mode = "reading"
system_block = "项目事实优先。"
""",
        encoding="utf-8",
    )
    persona = load_persona_catalog(
        Settings(cowork_data_path=tmp_path / "data"), project_roots=(project,)
    ).get("researcher")
    registry = build_default_cowork_registry()
    state = cast(
        "CoworkState",
        cast(
            "dict[str, Any]",
            {
                "capability_exclusive": False,
                "capability_tools": [],
                "persona_tool_patterns": list(persona.tool_patterns),
            },
        ),
    )
    allowed = _scoped_allowed_tools(state, registry)

    assert persona.origin == "project"
    assert persona.label == "项目研究员"
    assert allowed is not None
    assert "read_file" in allowed
    assert "write_file" not in allowed
    # 旧 checkpoint 若已调用过别名，Persona 仍允许它继续回放。
    assert "read_text_file" in allowed
    # Persona 只收窄业务工具，申请目录/能力与向用户提问的安全控制面永远保留。
    assert {"ask_user", "request_directory", "request_capability"} <= allowed

    assert (
        approval_mode_for_persona_change(
            current_name="general", requested_mode="auto", selected=persona
        )
        == "interactive"
    )
    assert (
        approval_mode_for_persona_change(
            current_name="researcher", requested_mode="auto", selected=persona
        )
        == "auto"
    )


async def test_selected_persona_persists_on_conversation(db_session: AsyncSession) -> None:
    conversation_id = await ensure_conversation(db_session, title="Persona")

    await update_conversation_runtime(
        db_session,
        conversation_id=conversation_id,
        provider_profile_id=None,
        model_override=None,
        unattended=False,
        approval_mode="interactive",
        persona_name="meeting-secretary",
    )
    selected = await get_conversation(db_session, conversation_id=conversation_id)

    assert selected is not None
    assert selected.persona_name == "meeting-secretary"
