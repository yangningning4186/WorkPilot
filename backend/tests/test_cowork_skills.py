from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.agent_core.budget import BudgetedGateway, BudgetMeter
from app.api.integrations import put_session_skill_mute
from app.core.config import Settings
from app.core.db import DbSession as AsyncSession
from app.cowork.extensions import (
    reconcile_skill_runtime_snapshot,
    register_skill_tools,
    render_skill_countermand,
)
from app.cowork.permissions import create_session_root
from app.cowork.runtime import initialize_cowork_state, run_cowork_graph
from app.cowork.skills.catalog import (
    BUILTIN_DISABLED_DIRNAME,
    BUILTIN_SKILLS_ROOT,
    SkillCatalog,
    SkillCatalogError,
    SkillDefinition,
    load_skill_catalog,
)
from app.cowork.skills.lifecycle import (
    install_auto_distilled_skill,
    list_managed_skills,
    remove_skill,
    set_skill_enabled,
)
from app.cowork.tools import (
    CoworkToolContext,
    CoworkToolError,
    build_default_cowork_registry,
)
from app.cowork_contracts import CapabilityDeniedError
from app.cowork_store.routing import cowork_store
from app.runstore.runs import create_run, ensure_conversation, finish_run
from app.schemas.skills import SkillSessionMuteRequest
from tests.fakes import DeterministicProvider
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.types import CompletionResult, Message, ToolDefinition


class _SkillResumeProvider(DeterministicProvider):
    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        del tools, parallel_tool_calls
        return await self.complete(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )


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
tools: [read_file]
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


def test_builtin_skill_bundles_expose_v2_kind_runtime_and_resources(tmp_path: Path) -> None:
    catalog = load_skill_catalog(tmp_path, max_files=100, max_bytes=100_000)

    pptx = catalog.get("pptx")
    office = catalog.get("office-workflow")
    assert (pptx.kind, pptx.runtime_profile) == ("artifact", "artifact-python")
    assert (office.kind, office.runtime_profile) == ("workflow", "none")
    with pytest.raises(SkillCatalogError, match="未知或未启用"):
        catalog.get("presentation-planning")

    managed = list_managed_skills(tmp_path, max_files=100, max_bytes=100_000)
    pptx_item = next(item for item in managed if item.name == "pptx" and item.origin == "builtin")
    office_item = next(
        item for item in managed if item.name == "office-workflow" and item.origin == "builtin"
    )
    assert pptx_item.resource_counts()["references"] >= 4
    assert pptx_item.resource_counts()["evals"] >= 1
    assert pptx_item.resource_counts()["scripts"] >= 3
    assert "scripts/render_pptx.py" in pptx_item.resources
    assert "scripts/pptx2image.py" in pptx_item.resources
    assert "scripts/pptxgenjs/render_pptx.cjs" in pptx_item.resources
    assert "scripts/pptxgenjs/components.cjs" in pptx_item.resources
    assert "assets/templates/catalog.json" in pptx_item.resources
    assert office_item.resource_counts()["references"] >= 4
    assert office_item.resource_counts()["evals"] >= 1


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
    assert registry.get("load_skill_resource").effect == "none"
    assert "contract-review" in registry.system_instructions()
    assert registry.runtime_snapshot()["skills"]["snapshot_sha256"] == (catalog.snapshot_sha256)
    ordinary_goal_tools = {tool.name for tool in registry.tool_definitions_for("整理当前工作目录")}
    assert {"run_shell", "load_skill", "load_skill_resource"} <= ordinary_goal_tools
    assert "list_skills" not in ordinary_goal_tools
    assert "list_skills" not in registry.deferred_tool_names()
    manifest = registry.deferred_tools_manifest()
    assert "list_skills" not in manifest
    assert "load_skill_resource" in registry.names()
    assert registry.requires_approval("model_hallucinated_tool") is False


async def test_skill_runtime_lists_and_reads_only_bounded_safe_resources(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    skill_file = _write_skill(tmp_path)
    references = skill_file.parent / "references"
    references.mkdir()
    (references / "checklist.md").write_text("先核对主体。", encoding="utf-8")
    (references / "oversized.md").write_text("x" * 1_025, encoding="utf-8")
    (references / "binary.bin").write_bytes(b"\xff\xfe")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (references / "escape.md").symlink_to(outside)

    settings = Settings(
        cowork_skills_path=tmp_path,
        cowork_skill_max_files=20,
        cowork_skill_max_bytes=1_024,
    )
    registry = build_default_cowork_registry()
    register_skill_tools(registry, settings)
    conversation_id = await ensure_conversation(db_session, title="Skill resources")
    context = CoworkToolContext(
        session=db_session,
        gateway=ModelGateway(DeterministicProvider(), embedding_dimensions=1_024),
        settings=settings,
        conversation_id=conversation_id,
        run_id=uuid4(),
        worker_id="skill-resource-test",
        plan_step_id=uuid4(),
        tool_call_id="skill-resource-call",
    )

    loaded = await registry.execute("load_skill", {"name": "contract-review"}, context=context)
    by_path = {item["path"]: item for item in loaded.output["resources"]}
    assert by_path["references/checklist.md"]["readable"] is True
    assert by_path["references/oversized.md"]["readable"] is False
    assert "references/escape.md" not in by_path
    assert loaded.output["resource_loader"] == "load_skill_resource"

    resource = await registry.execute(
        "load_skill_resource",
        {"name": "contract-review", "resource": "references/checklist.md"},
        context=context,
    )
    assert resource.output["content"] == "先核对主体。"
    assert resource.output["resource"] == "references/checklist.md"

    for unsafe in (
        "../outside.txt",
        "references/escape.md",
        "references/oversized.md",
        "references/binary.bin",
    ):
        with pytest.raises(CoworkToolError):
            await registry.execute(
                "load_skill_resource",
                {"name": "contract-review", "resource": unsafe},
                context=context,
            )


@pytest.mark.parametrize("drift", ["modified", "deleted", "disabled", "muted"])
async def test_loaded_skill_identity_drift_is_countermanded_until_reload(
    db_session: AsyncSession,
    tmp_path: Path,
    drift: str,
) -> None:
    skill_file = _write_skill(tmp_path)
    settings = Settings(cowork_skills_path=tmp_path)
    conversation_id = await ensure_conversation(db_session, title=f"Skill {drift}")
    first_registry = build_default_cowork_registry()
    register_skill_tools(first_registry, settings)
    context = CoworkToolContext(
        session=db_session,
        gateway=ModelGateway(DeterministicProvider(), embedding_dimensions=1_024),
        settings=settings,
        conversation_id=conversation_id,
        run_id=uuid4(),
        worker_id="skill-drift-test",
        plan_step_id=uuid4(),
        tool_call_id="load-old-skill",
    )
    old_result = await first_registry.execute(
        "load_skill", {"name": "contract-review"}, context=context
    )
    previous_snapshot = first_registry.runtime_snapshot()
    old_loaded = previous_snapshot["skills"]["loaded"]

    assert old_loaded == [
        {
            "name": "contract-review",
            "origin": "user",
            "source_identity": f"user:{tmp_path}:contract-review/SKILL.md",
            "sha256": old_result.output["sha256"],
        }
    ]

    muted_names = frozenset({"contract-review"}) if drift == "muted" else frozenset()
    if drift == "modified":
        skill_file.write_text(
            skill_file.read_text(encoding="utf-8").replace("违约责任", "争议解决"),
            encoding="utf-8",
        )
    elif drift == "deleted":
        remove_skill(tmp_path, name="contract-review")
    elif drift == "disabled":
        set_skill_enabled(
            tmp_path,
            name="contract-review",
            enabled=False,
            max_bytes=20_000,
        )

    current_registry = build_default_cowork_registry()
    current_catalog = register_skill_tools(
        current_registry,
        settings,
        muted_skill_names=muted_names,
    )
    block = reconcile_skill_runtime_snapshot(current_registry, previous_snapshot)
    skills_state = current_registry.runtime_snapshot()["skills"]
    listed = await current_registry.execute("list_skills", {}, context=context)
    listed_names = {item["name"] for item in listed.output["skills"]}

    assert skills_state["loaded"] == []
    assert [item["name"] for item in skills_state["invalidated"]] == ["contract-review"]
    assert "<skill_countermand>" in block
    assert render_skill_countermand(current_registry.runtime_snapshot()) == block
    assert ("contract-review" in listed_names) is (drift == "modified")
    assert ("contract-review" in {item.name for item in current_catalog.skills}) is (
        drift == "modified"
    )
    with pytest.raises(CoworkToolError, match=r"重新调用 load_skill|不存在"):
        await current_registry.execute(
            "load_skill_resource",
            {"name": "contract-review", "resource": "references/checklist.md"},
            context=context,
        )

    if drift == "modified":
        new_result = await current_registry.execute(
            "load_skill", {"name": "contract-review"}, context=context
        )
        assert new_result.output["sha256"] != old_result.output["sha256"]
        assert current_registry.runtime_snapshot()["skills"]["invalidated"] == []
        assert render_skill_countermand(current_registry.runtime_snapshot()) == ""
    else:
        with pytest.raises(CoworkToolError, match="不存在"):
            await current_registry.execute(
                "load_skill", {"name": "contract-review"}, context=context
            )


async def test_loaded_skill_drift_is_reconciled_before_resuming_a_checkpoint(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    skill_file = _write_skill(skills_root)
    settings = Settings(cowork_skills_path=skills_root)
    conversation_id = await ensure_conversation(db_session, title="Skill resume drift")
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="继续处理合同",
        budget_tokens=100_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    first_registry = build_default_cowork_registry()
    register_skill_tools(first_registry, settings)
    state = await initialize_cowork_state(
        db_session,
        run_id=run.id,
        registry=first_registry,
        settings=settings,
    )
    context = CoworkToolContext(
        session=db_session,
        gateway=ModelGateway(DeterministicProvider(), embedding_dimensions=1_024),
        settings=settings,
        conversation_id=conversation_id,
        run_id=run.id,
        worker_id="skill-resume",
        plan_step_id=uuid4(),
        tool_call_id="load-before-pause",
    )
    await first_registry.execute("load_skill", {"name": "contract-review"}, context=context)
    state["runtime_snapshot"] = first_registry.runtime_snapshot()
    initial = await cowork_store().load_latest_checkpoint(run_id=run.id)
    assert initial is not None
    await cowork_store().save_checkpoint(
        run_id=run.id,
        state=dict(state),
        parent_id=initial.checkpoint_id,
    )

    skill_file.write_text(
        skill_file.read_text(encoding="utf-8").replace("违约责任", "争议解决"),
        encoding="utf-8",
    )
    current_registry = build_default_cowork_registry()
    register_skill_tools(current_registry, settings)
    claimed = await cowork_store().claim_run(
        run_id=run.id,
        worker_id="skill-resume",
        lease_s=60,
    )
    assert claimed is not None
    provider = _SkillResumeProvider(completion_text="已停止依赖旧 Skill。")
    meter = BudgetMeter(
        dict(state["budget"]),
        chars_per_token=settings.cost_estimate_chars_per_token,
    )

    resumed = await run_cowork_graph(
        db_session,
        run_id=run.id,
        registry=current_registry,
        gateway=BudgetedGateway(
            ModelGateway(provider, embedding_dimensions=1_024),
            meter,
        ),
        meter=meter,
        settings=settings,
        worker_id="skill-resume",
    )

    assert resumed["status"] == "done"
    assert "contract-review" in resumed["skill_countermand_block"]
    assert resumed["runtime_snapshot"]["skills"]["loaded"] == []
    assert any(
        message.role == "system" and "<skill_countermand>" in message.content
        for message in provider.last_messages
    )


async def test_runtime_requires_the_registry_and_session_mutes_to_match(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path)
    settings = Settings(cowork_skills_path=tmp_path)
    conversation_id = await ensure_conversation(db_session, title="Skill mute binding")
    accepted_run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="不使用合同审阅 Skill",
        budget_tokens=100_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    accepted_registry = build_default_cowork_registry()
    register_skill_tools(
        accepted_registry,
        settings,
        muted_skill_names=frozenset({"contract-review"}),
    )

    accepted = await initialize_cowork_state(
        db_session,
        run_id=accepted_run.id,
        registry=accepted_registry,
        settings=settings,
        muted_skill_names=frozenset({"contract-review"}),
    )

    assert accepted["runtime_snapshot"]["skills"]["muted_names"] == ["contract-review"]
    rejected_run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="mute receipt 丢失",
        budget_tokens=100_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    rejected_registry = build_default_cowork_registry()
    register_skill_tools(
        rejected_registry,
        settings,
        muted_skill_names=frozenset({"contract-review"}),
    )
    with pytest.raises(ValueError, match="effective catalog"):
        await initialize_cowork_state(
            db_session,
            run_id=rejected_run.id,
            registry=rejected_registry,
            settings=settings,
        )


def test_prompt_catalog_includes_every_skill_without_character_truncation(tmp_path: Path) -> None:
    skills = tuple(
        SkillDefinition(
            name=f"skill-{index:02d}",
            description=f"第 {index} 项 " + ("长描述" * 300),
            trigger=(f"触发 {index}",),
            anti_trigger=(f"排除 {index}",),
            tools=("read_file",),
            procedure="一步。",
            source_path=tmp_path / f"skill-{index:02d}" / "SKILL.md",
            sha256=f"{index:064x}",
        )
        for index in range(20)
    )

    prompt = SkillCatalog(skills=skills, snapshot_sha256="0" * 64).prompt_catalog()

    assert len(prompt) > 12_000
    assert "skill-00" in prompt
    assert "skill-19" in prompt
    assert "另有" not in prompt
    assert "list_skills" not in prompt


# --- 出厂 Skill 层 -------------------------------------------------------------


def _write_named_skill(root: Path, name: str, description: str) -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"""---
name: {name}
description: {description}
trigger: [随便]
tools: [read_file]
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
    for name in ("docx", "xlsx", "pptx"):
        procedure = catalog.get(name).procedure
        assert "不得创建辅助脚本、备份或产物" in procedure
        assert "python -c" in procedure


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


def test_disabling_a_fork_denies_the_name_instead_of_reviving_builtin(tmp_path: Path) -> None:
    """停用是名称级决定，低优先级同名实现不得自动复活。"""

    _write_named_skill(tmp_path, "skill-creator", "我自己那份")

    result = set_skill_enabled(tmp_path, name="skill-creator", enabled=False, max_bytes=20_000)

    assert result.origin == "user"
    assert (tmp_path / BUILTIN_DISABLED_DIRNAME / "skill-creator").is_file()
    catalog = load_skill_catalog(tmp_path, max_files=20, max_bytes=20_000)
    assert "skill-creator" not in {skill.name for skill in catalog.skills}


def test_name_denylist_also_blocks_project_override(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_skill = project_root / ".workpilot" / "skills" / "skill-creator"
    _write_named_skill(project_skill.parent, "skill-creator", "项目层")

    set_skill_enabled(tmp_path / "user", name="skill-creator", enabled=False, max_bytes=20_000)
    catalog = load_skill_catalog(
        tmp_path / "user",
        max_files=20,
        max_bytes=20_000,
        project_roots=(project_root,),
    )

    assert "skill-creator" not in {skill.name for skill in catalog.skills}


def test_conversation_mute_filters_the_same_catalog_used_by_runtime(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_skill = project_root / ".workpilot" / "skills" / "skill-creator"
    _write_named_skill(project_skill.parent, "skill-creator", "项目层")

    muted = load_skill_catalog(
        tmp_path / "user",
        max_files=20,
        max_bytes=20_000,
        project_roots=(project_root,),
        muted_names=frozenset({"skill-creator"}),
    )
    unmuted = load_skill_catalog(
        tmp_path / "user",
        max_files=20,
        max_bytes=20_000,
        project_roots=(project_root,),
    )

    assert "skill-creator" not in {skill.name for skill in muted.skills}
    assert unmuted.get("skill-creator").origin == "project"


def test_conversation_mute_cannot_reenable_a_globally_disabled_name(tmp_path: Path) -> None:
    set_skill_enabled(tmp_path, name="skill-creator", enabled=False, max_bytes=20_000)

    catalog = load_skill_catalog(
        tmp_path,
        max_files=20,
        max_bytes=20_000,
        muted_names=frozenset(),
    )

    assert "skill-creator" not in {skill.name for skill in catalog.skills}


async def test_conversation_skill_mute_is_persisted_visible_and_immutable_during_runs(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    _write_skill(skills_root)
    settings = Settings(cowork_skills_path=skills_root)
    conversation_id = await ensure_conversation(db_session, title="Session skill policy")

    muted = await put_session_skill_mute(
        conversation_id=conversation_id,
        skill_name="contract-review",
        request=SkillSessionMuteRequest(muted=True),
        settings=settings,
        session=db_session,
    )
    assert muted["muted_names"] == ["contract-review"]
    assert {item["name"] for item in muted["available_skills"]} >= {"contract-review"}
    assert "contract-review" not in {item["name"] for item in muted["skills"]}
    assert await cowork_store().list_conversation_skill_mutes(
        conversation_id=conversation_id
    ) == frozenset({"contract-review"})

    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="active run freezes the effective Skill catalog",
        budget_tokens=10_000,
        budget_calls=10,
        budget_wall_ms=60_000,
        workflow_type="cowork",
    )
    with pytest.raises(HTTPException) as excinfo:
        await put_session_skill_mute(
            conversation_id=conversation_id,
            skill_name="contract-review",
            request=SkillSessionMuteRequest(muted=False),
            settings=settings,
            session=db_session,
        )
    assert excinfo.value.status_code == 409

    assert await finish_run(db_session, run_id=run.id, status="cancelled")
    unmuted = await put_session_skill_mute(
        conversation_id=conversation_id,
        skill_name="contract-review",
        request=SkillSessionMuteRequest(muted=False),
        settings=settings,
        session=db_session,
    )
    assert unmuted["muted_names"] == []
    assert "contract-review" in {item["name"] for item in unmuted["skills"]}


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


def test_removing_a_disabled_fork_does_not_clear_the_name_denylist(tmp_path: Path) -> None:
    _write_named_skill(tmp_path, "skill-creator", "我自己那份")
    set_skill_enabled(tmp_path, name="skill-creator", enabled=False, max_bytes=20_000)

    remove_skill(tmp_path, name="skill-creator")

    catalog = load_skill_catalog(tmp_path, max_files=20, max_bytes=20_000)
    assert "skill-creator" not in {skill.name for skill in catalog.skills}
    assert (tmp_path / BUILTIN_DISABLED_DIRNAME / "skill-creator").is_file()


def test_auto_distillation_never_shadows_a_builtin(tmp_path: Path) -> None:
    """自动晋升拿走出厂名字 = 用一段没人审过的正文换掉出厂流程，而列表里看不出来。"""

    with pytest.raises(FileExistsError, match="出厂"):
        install_auto_distilled_skill(
            tmp_path,
            name="skill-creator",
            capability_key="skill-creator",
            skill_md="---\nname: skill-creator\ndescription: x\nstatus: active\n---\n\n一步。\n",
            max_bytes=20_000,
            provenance_signing_key="test-provenance-key",
        )


def test_list_managed_skills_keeps_the_shadowed_builtin_visible(tmp_path: Path) -> None:
    _write_named_skill(tmp_path, "skill-creator", "我自己那份")

    items = list_managed_skills(tmp_path, max_files=50, max_bytes=20_000)

    by_origin = {(item.name, item.origin): item for item in items}
    assert by_origin[("skill-creator", "builtin")].shadowed is True
    assert by_origin[("skill-creator", "builtin")].public()["removable"] is False
    assert by_origin[("skill-creator", "user")].public()["removable"] is True


def test_project_skill_overrides_user_and_builtin(tmp_path: Path) -> None:
    user_root = tmp_path / "user"
    project_root = tmp_path / "project"
    _write_named_skill(user_root, "skill-creator", "用户层")
    project_skill = project_root / ".workpilot" / "skills" / "skill-creator"
    _write_named_skill(project_skill.parent, "skill-creator", "项目层")
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


async def test_project_skill_resource_uses_catalog_winner_and_workspace_authorization(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    user_root = tmp_path / "user-runtime"
    project_root = tmp_path / "project-runtime"
    _write_named_skill(user_root, "skill-creator", "用户层")
    project_skill = project_root / ".workpilot" / "skills" / "skill-creator"
    _write_named_skill(project_skill.parent, "skill-creator", "项目层")
    for directory, content in (
        (user_root / "skill-creator", "user resource"),
        (project_skill, "project resource"),
    ):
        (directory / "references").mkdir()
        (directory / "references" / "source.md").write_text(content, encoding="utf-8")

    settings = Settings(cowork_skills_path=user_root)
    registry = build_default_cowork_registry()
    register_skill_tools(registry, settings, project_roots=(project_root,))
    conversation_id = await ensure_conversation(db_session, title="Project skill resource")
    context = CoworkToolContext(
        session=db_session,
        gateway=ModelGateway(DeterministicProvider(), embedding_dimensions=1_024),
        settings=settings,
        conversation_id=conversation_id,
        run_id=uuid4(),
        worker_id="project-skill-resource-test",
        plan_step_id=uuid4(),
        tool_call_id="project-skill-resource-call",
    )

    with pytest.raises(CapabilityDeniedError):
        await registry.execute("load_skill", {"name": "skill-creator"}, context=context)

    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(project_root),
        access_mode="read_only",
    )
    loaded = await registry.execute("load_skill", {"name": "skill-creator"}, context=context)
    assert loaded.output["origin"] == "project"
    resource = await registry.execute(
        "load_skill_resource",
        {"name": "skill-creator", "resource": "references/source.md"},
        context=context,
    )
    assert resource.output["content"] == "project resource"
