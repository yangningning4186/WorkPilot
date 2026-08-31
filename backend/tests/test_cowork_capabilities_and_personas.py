from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from app.api.conversations import put_conversation_runtime
from app.core.config import Settings
from app.core.db import DbSession as AsyncSession
from app.cowork.capabilities import (
    CapabilityActivation,
    CapabilityPreLoopContext,
    WorkCapability,
    WorkCapabilityRegistry,
    build_work_capability_registry,
)
from app.cowork.permissions import create_session_root, revoke_session_root
from app.cowork.personas import (
    PERSONA_RESELECTION_REQUIRED,
    approval_mode_for_persona_change,
    load_persona_catalog,
    render_persona_system_block,
    snapshot_persona,
)
from app.cowork.runtime import (
    CoworkState,
    _scoped_allowed_tools,
    initialize_cowork_state,
    load_cowork_checkpoint,
)
from app.cowork.tools import build_default_cowork_registry
from app.runstore.conversations import (
    get_conversation,
    update_conversation_runtime,
)
from app.runstore.runs import create_run, ensure_conversation, finish_run
from app.schemas.conversations import ConversationRuntimeUpdate
from app.schemas.personas import PersonaResponse


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


def test_builtin_expert_council_exposes_pinned_member_profiles(tmp_path: Path) -> None:
    settings = Settings(cowork_data_path=tmp_path / "data")
    persona = load_persona_catalog(settings).get("expert-council")

    assert persona.expert_type == "team"
    assert [member.profile for member in persona.team_members] == [
        "evidence-researcher",
        "domain-analyst",
        "critical-reviewer",
    ]
    assert persona.public()["team_members"][0]["label"] == "证据研究专家"
    response = PersonaResponse.model_validate(persona.public())
    assert response.expert_type == "team"
    assert response.team_members[2].profile == "critical-reviewer"

    snapshot = snapshot_persona(persona, settings)
    assert snapshot["expert_type"] == "team"
    assert [member["profile"] for member in snapshot["team_members"]] == [
        "evidence-researcher",
        "domain-analyst",
        "critical-reviewer",
    ]
    assert all(len(member["sha256"]) == 64 for member in snapshot["team_members"])
    # Checkpoint 快照只保留身份、边界与摘要，不复制成员 prompt 正文。
    assert "你是证据研究专家" not in str(snapshot)
    runtime_block = render_persona_system_block(persona, snapshot)
    assert snapshot["sha256"] in runtime_block
    assert "<expert_team_manifest>" in runtime_block


def test_project_expert_team_persona_parses_members_and_tool_boundaries(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    persona_root = project / ".workpilot" / "personas"
    persona_root.mkdir(parents=True)
    (persona_root / "release-council.toml").write_text(
        """name = "release-council"
label = "发布专家团"
description = "用实现与独立复核两个角色检查发布"
expert_type = "team"
tool_patterns = []
default_approval_mode = "interactive"
recommended_connectors = []
recommended_work_mode = "office"
system_block = "按阶段组织发布会诊。"

[[team_members]]
profile = "implementer"
label = "实现专家"
role = "检查实现与交付证据"
reason = "建立实现侧证据"
system_block = "只报告实际实现证据。"
tool_patterns = ["read_*", "search_files"]

[[team_members]]
profile = "reviewer"
label = "复核专家"
role = "独立检查实现结论"
reason = "避免自验收"
system_block = "寻找反例并报告证据缺口。"
tool_patterns = ["read_*"]
""",
        encoding="utf-8",
    )

    persona = load_persona_catalog(
        Settings(cowork_data_path=tmp_path / "data"),
        project_roots=(project,),
    ).get("release-council")

    assert persona.origin == "project"
    assert persona.expert_type == "team"
    assert persona.team_members[0].tool_patterns == ("read_*", "search_files")


def test_plain_persona_snapshot_remains_v1_compatible(tmp_path: Path) -> None:
    settings = Settings(cowork_data_path=tmp_path / "data")
    snapshot = snapshot_persona(load_persona_catalog(settings).get("general"), settings)

    assert snapshot["schema_version"] == "workpilot.persona-snapshot.v1"
    assert "expert_type" not in snapshot
    assert "team_members" not in snapshot


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


def _write_project_persona(
    project: Path,
    *,
    name: str = "bounded-reviewer",
    system_block: str = "只根据项目证据审阅。",
) -> Path:
    path = project / ".workpilot" / "personas" / f"{name}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'''name = "{name}"
label = "受限审阅员"
description = "只允许读取和搜索已授权项目"
tool_patterns = ["read_*", "search_*"]
default_approval_mode = "interactive"
recommended_connectors = ["feishu"]
recommended_work_mode = "reading"
system_block = "{system_block}"
''',
        encoding="utf-8",
    )
    return path


async def _select_persona(
    session: AsyncSession,
    *,
    conversation_id: Any,
    persona_name: str,
) -> None:
    await update_conversation_runtime(
        session,
        conversation_id=conversation_id,
        provider_profile_id=None,
        model_override=None,
        unattended=False,
        approval_mode="interactive",
        persona_name=persona_name,
    )


async def _new_run(session: AsyncSession, *, conversation_id: Any, goal: str) -> Any:
    return await create_run(
        session,
        conversation_id=conversation_id,
        goal=goal,
        budget_tokens=10_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )


async def test_selected_expert_council_freezes_manifest_into_runtime_prompt(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    settings = Settings(cowork_data_path=tmp_path / "data")
    conversation_id = await ensure_conversation(db_session, title="Expert manifest")
    await _select_persona(
        db_session,
        conversation_id=conversation_id,
        persona_name="expert-council",
    )
    run = await _new_run(
        db_session, conversation_id=conversation_id, goal="启动深度研究与风险评审团"
    )

    state = await initialize_cowork_state(
        db_session,
        run_id=run.id,
        registry=build_default_cowork_registry(),
        settings=settings,
    )

    snapshot = state["persona_snapshot"]
    assert snapshot is not None
    assert snapshot["sha256"] in state["persona_block"]
    assert '"expert":"expert-council"' in state["persona_block"]
    assert "<expert_team_manifest>" in state["persona_block"]


async def test_first_run_persists_canonical_persona_snapshot(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    settings = Settings(cowork_data_path=tmp_path / "data")
    project = tmp_path / "repo"
    _write_project_persona(project)
    conversation_id = await ensure_conversation(db_session, title="Persona snapshot")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(project),
        access_mode="read_only",
    )
    await _select_persona(
        db_session,
        conversation_id=conversation_id,
        persona_name="bounded-reviewer",
    )
    run = await _new_run(db_session, conversation_id=conversation_id, goal="审阅项目")

    state = await initialize_cowork_state(
        db_session,
        run_id=run.id,
        registry=build_default_cowork_registry(),
        settings=settings,
    )
    checkpoint = await load_cowork_checkpoint(db_session, run_id=run.id)

    assert checkpoint is not None
    assert checkpoint.state["persona_snapshot"] == state["persona_snapshot"]
    snapshot = state["persona_snapshot"]
    assert snapshot is not None
    assert snapshot["schema_version"] == "workpilot.persona-snapshot.v1"
    assert snapshot["name"] == "bounded-reviewer"
    assert snapshot["origin"] == "project"
    assert snapshot["source_identity"].endswith(":.workpilot/personas/bounded-reviewer.toml")
    assert len(snapshot["sha256"]) == 64
    assert snapshot["tool_patterns"] == ["read_*", "search_*"]
    assert snapshot["capability_summary"]["recommended_connectors"] == [
        {
            "kind": "feishu",
            "capabilities": [
                "openapi",
                "calendar",
                "base",
                "docs",
                "drive",
                "tasks",
                "approval",
            ],
        }
    ]


@pytest.mark.parametrize("drift", ["content", "missing", "authorization"])
async def test_same_name_persona_drift_or_lost_authorization_requires_reselection(
    db_session: AsyncSession,
    tmp_path: Path,
    drift: str,
) -> None:
    settings = Settings(cowork_data_path=tmp_path / "data")
    project = tmp_path / "repo"
    persona_path = _write_project_persona(project)
    conversation_id = await ensure_conversation(db_session, title=f"Persona {drift}")
    root = await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(project),
        access_mode="read_only",
    )
    await _select_persona(
        db_session,
        conversation_id=conversation_id,
        persona_name="bounded-reviewer",
    )
    first = await _new_run(db_session, conversation_id=conversation_id, goal="第一次审阅")
    await initialize_cowork_state(
        db_session,
        run_id=first.id,
        registry=build_default_cowork_registry(),
        settings=settings,
    )
    assert await finish_run(db_session, run_id=first.id, status="done")

    if drift == "content":
        _write_project_persona(project, system_block="已被修改的 Persona 指令。")
    elif drift == "missing":
        persona_path.unlink()
    else:
        assert await revoke_session_root(
            db_session,
            conversation_id=conversation_id,
            root_id=root.id,
        )
    second = await _new_run(db_session, conversation_id=conversation_id, goal="再次审阅")

    with pytest.raises(ValueError, match=f"^{PERSONA_RESELECTION_REQUIRED}$"):
        await initialize_cowork_state(
            db_session,
            run_id=second.id,
            registry=build_default_cowork_registry(),
            settings=settings,
        )
    assert await load_cowork_checkpoint(db_session, run_id=second.id) is None


async def test_explicit_persona_change_captures_a_new_snapshot(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    settings = Settings(cowork_data_path=tmp_path / "data")
    project = tmp_path / "repo"
    _write_project_persona(project)
    conversation_id = await ensure_conversation(db_session, title="Persona explicit change")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(project),
        access_mode="read_only",
    )
    await _select_persona(
        db_session,
        conversation_id=conversation_id,
        persona_name="bounded-reviewer",
    )
    first = await _new_run(db_session, conversation_id=conversation_id, goal="项目审阅")
    first_state = await initialize_cowork_state(
        db_session,
        run_id=first.id,
        registry=build_default_cowork_registry(),
        settings=settings,
    )
    assert await finish_run(db_session, run_id=first.id, status="done")

    await _select_persona(db_session, conversation_id=conversation_id, persona_name="general")
    second = await _new_run(db_session, conversation_id=conversation_id, goal="切换为通用执行")
    second_state = await initialize_cowork_state(
        db_session,
        run_id=second.id,
        registry=build_default_cowork_registry(),
        settings=settings,
    )

    assert first_state["persona_snapshot"] is not None
    assert second_state["persona_snapshot"] is not None
    assert first_state["persona_snapshot"]["name"] == "bounded-reviewer"
    assert second_state["persona_snapshot"]["name"] == "general"
    assert first_state["persona_snapshot"]["sha256"] != second_state["persona_snapshot"]["sha256"]


async def test_same_name_explicit_reselection_accepts_the_current_definition(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    settings = Settings(cowork_data_path=tmp_path / "data")
    project = tmp_path / "repo"
    _write_project_persona(project)
    conversation_id = await ensure_conversation(db_session, title="Persona same-name reselect")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(project),
        access_mode="read_only",
    )
    await _select_persona(
        db_session,
        conversation_id=conversation_id,
        persona_name="bounded-reviewer",
    )
    first = await _new_run(db_session, conversation_id=conversation_id, goal="第一次审阅")
    first_state = await initialize_cowork_state(
        db_session,
        run_id=first.id,
        registry=build_default_cowork_registry(),
        settings=settings,
    )
    assert await finish_run(db_session, run_id=first.id, status="done")

    _write_project_persona(project, system_block="用户重新确认后的新版 Persona。")
    rejected = await _new_run(db_session, conversation_id=conversation_id, goal="发现漂移")
    with pytest.raises(ValueError, match=f"^{PERSONA_RESELECTION_REQUIRED}$"):
        await initialize_cowork_state(
            db_session,
            run_id=rejected.id,
            registry=build_default_cowork_registry(),
            settings=settings,
        )
    assert await finish_run(
        db_session,
        run_id=rejected.id,
        status="failed",
        error=PERSONA_RESELECTION_REQUIRED,
    )

    # PUT 同一个 Persona 且不夹带 provider/model/权限档变化，是明确的重新选择动作。
    await put_conversation_runtime(
        conversation_id=conversation_id,
        request=ConversationRuntimeUpdate(
            provider_profile_id=None,
            model_override=None,
            unattended=False,
            approval_mode="interactive",
            persona_name="bounded-reviewer",
        ),
        session=db_session,
        settings=settings,
        _=None,
    )
    accepted = await _new_run(db_session, conversation_id=conversation_id, goal="重新审阅")
    accepted_state = await initialize_cowork_state(
        db_session,
        run_id=accepted.id,
        registry=build_default_cowork_registry(),
        settings=settings,
    )

    assert first_state["persona_snapshot"] is not None
    assert accepted_state["persona_snapshot"] is not None
    assert accepted_state["persona_snapshot"]["sha256"] != first_state["persona_snapshot"]["sha256"]


async def test_provider_only_runtime_update_does_not_authorize_persona_drift(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    settings = Settings(cowork_data_path=tmp_path / "data")
    project = tmp_path / "repo"
    _write_project_persona(project)
    conversation_id = await ensure_conversation(db_session, title="Persona provider isolation")
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(project),
        access_mode="read_only",
    )
    await _select_persona(
        db_session,
        conversation_id=conversation_id,
        persona_name="bounded-reviewer",
    )
    first = await _new_run(db_session, conversation_id=conversation_id, goal="第一次审阅")
    await initialize_cowork_state(
        db_session,
        run_id=first.id,
        registry=build_default_cowork_registry(),
        settings=settings,
    )
    assert await finish_run(db_session, run_id=first.id, status="done")
    _write_project_persona(project, system_block="未经重新选择的漂移内容。")

    await put_conversation_runtime(
        conversation_id=conversation_id,
        request=ConversationRuntimeUpdate(
            provider_profile_id=None,
            model_override="provider-only-model-change",
            unattended=False,
            approval_mode="interactive",
            persona_name="bounded-reviewer",
        ),
        session=db_session,
        settings=settings,
        _=None,
    )
    second = await _new_run(db_session, conversation_id=conversation_id, goal="再次审阅")

    with pytest.raises(ValueError, match=f"^{PERSONA_RESELECTION_REQUIRED}$"):
        await initialize_cowork_state(
            db_session,
            run_id=second.id,
            registry=build_default_cowork_registry(),
            settings=settings,
        )
