"""Cowork 当前 outbound 上下文的只读估算。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from app.agent_core.compaction import (
    build_outbound_messages,
    normalize_compaction_state,
)
from app.core.config import Settings
from app.core.db import DbSession as AsyncSession
from app.cowork.automation_tools import register_scheduler_tools
from app.cowork.browser_tools import register_browser_tools
from app.cowork.capabilities import CapabilityActivation, build_work_capability_registry
from app.cowork.connector_tools import connected_connector_kinds, register_connector_tools
from app.cowork.environment import render_roots_block, render_workspace_files_block
from app.cowork.extensions import register_skill_tools
from app.cowork.memory_tools import register_memory_tools
from app.cowork.permissions import list_session_roots
from app.cowork.plans import CoworkMode, normalize_mode
from app.cowork.provider_profiles import get_provider_profile
from app.cowork.rag_tools import register_rag_tools
from app.cowork.runtime import (
    COWORK_COMPACTION_PROMPTS,
    _deferred_tools_block,
    _ephemeral_context,
    _scoped_allowed_tools,
    _system_prompt,
)
from app.cowork.skills.catalog import builtin_disabled_names
from app.cowork.subagent import register_readonly_subagent
from app.cowork.todos import TodoItem, normalize_todos
from app.cowork.tools import CoworkToolRegistry, build_default_cowork_registry
from app.cowork.work_modes import render_reading_viewport_block
from app.cowork_store.routing import cowork_store
from app.knowledge_contracts import RagService
from workpilot_ai.gateway import PromptBudget, request_character_count
from workpilot_ai.types import Message, ToolDefinition

_CONTEXT_REGISTRY_CACHE: tuple[tuple[Any, ...], CoworkToolRegistry] | None = None


def _context_registry(
    settings: Settings,
    *,
    rag: RagService,
    project_roots: tuple[Path, ...] = (),
) -> CoworkToolRegistry:
    """缓存只读上下文估算使用的工具 schema；Skill 文件变化时自动失效。

    `rag` 由调用方注入而不是在这里 `local_kb_service(settings)`：`app.cowork` 不许
    import `app.rag`（ADR-0011 契约 6，无例外）。装配 RAG 实现是入口适配层的职责，
    和 `worker/cowork_run.py` 里那次装配是同一个理由、同一份 `RagService` Protocol。
    """

    global _CONTEXT_REGISTRY_CACHE
    skills_root = settings.cowork_skills_path.expanduser()
    skill_revision = tuple(
        sorted(
            (str(path), path.stat().st_mtime_ns, path.stat().st_size)
            for path in skills_root.glob("*/SKILL.md")
            if path.is_file()
        )
    )
    # 出厂 Skill 随代码走、进程内不会变，所以不进 revision；但**停用标记**会变，
    # 少了它，关掉一个出厂 Skill 之后上下文估算还按开着的算。
    builtin_revision = tuple(sorted(builtin_disabled_names(skills_root)))
    connector_kinds = connected_connector_kinds(settings)
    project_revision = tuple(
        sorted(
            (str(path), path.stat().st_mtime_ns, path.stat().st_size)
            for root in project_roots
            for path in (root / ".workpilot" / "skills").glob("*/SKILL.md")
            if path.is_file()
        )
    )
    key = (
        str(skills_root),
        str(settings.knowledge_base_path.expanduser()),
        settings.cowork_skill_max_files,
        settings.cowork_skill_max_bytes,
        skill_revision,
        builtin_revision,
        project_revision,
        tuple(sorted(connector_kinds)),
    )
    if _CONTEXT_REGISTRY_CACHE is not None and _CONTEXT_REGISTRY_CACHE[0] == key:
        return _CONTEXT_REGISTRY_CACHE[1]
    registry = build_default_cowork_registry()
    register_skill_tools(registry, settings, project_roots=project_roots)
    register_browser_tools(registry)
    register_connector_tools(registry, enabled_kinds=connector_kinds)
    register_scheduler_tools(registry)
    register_memory_tools(registry)
    register_readonly_subagent(registry)
    register_rag_tools(registry, rag)
    _CONTEXT_REGISTRY_CACHE = (key, registry)
    return registry


def _message_tokens(message: Message) -> int:
    return request_character_count([message]) + 4


def _tool_definition_tokens(tools: list[ToolDefinition]) -> int:
    return request_character_count([], tools)


async def get_cowork_context_usage(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    settings: Settings,
    rag: RagService,
) -> dict[str, Any]:
    store = cowork_store()
    local_metadata = (
        []
        if store is None
        else await store.list_conversation_metadata(
            conversation_id=conversation_id, archived=None, limit=1
        )
    )
    selection = (
        local_metadata[0]["provider_profile_id"],
        local_metadata[0]["model_override"],
    )
    raw_profile_id, model_override = selection
    # Profile 出了数据库，这里少了一条 LEFT JOIN：id 悬空（用户删了 profile）时，
    # 用部署级窗口仅做占用估算；模型身份必须明确显示为未配置，不能暗示会回落调用。
    profile = (
        None
        if raw_profile_id is None
        else get_provider_profile(settings, UUID(str(raw_profile_id)))
    )
    conversation: dict[str, Any] = {
        "context_window": (
            settings.tier_main_context_window_tokens
            if profile is None
            else profile.context_window_tokens
        ),
        "model": model_override
        or (None if profile is None else profile.default_model)
        or "未配置模型",
    }

    local_run = await store.get_latest_run(conversation_id=conversation_id)
    local_checkpoint = (
        None if local_run is None else await store.load_latest_checkpoint(run_id=local_run.id)
    )
    latest: dict[str, Any] | Any | None = (
        None
        if local_run is None
        else {
            "id": local_run.id,
            "goal": local_run.goal,
            "status": local_run.status,
            "state": None if local_checkpoint is None else local_checkpoint.state,
        }
    )

    canonical: list[dict[str, Any]] = []
    compaction = normalize_compaction_state(None, message_count=0)
    goal = ""
    run_status: str | None = None
    runtime_snapshot: dict[str, Any] = {}
    todos: list[TodoItem] = []
    environment_block = ""
    memory_block = ""
    persona_block = ""
    mode_block = ""
    locate_block = ""
    knowledge_block = ""
    mode = "execute"
    reading_viewport: Any = None
    capability_tools: list[str] = []
    capability_exclusive = False
    persona_tool_patterns: list[str] = []
    workspace_files: list[str] = []
    if latest is not None:
        goal = str(latest["goal"])
        run_status = str(latest["status"])
        state = latest["state"]
        if isinstance(state, dict) and isinstance(state.get("messages"), list):
            canonical = cast(
                "list[dict[str, Any]]",
                json.loads(json.dumps(state["messages"], ensure_ascii=False)),
            )
            compaction = normalize_compaction_state(
                state.get("compaction"),
                message_count=len(canonical),
            )
            if isinstance(state.get("runtime_snapshot"), dict):
                runtime_snapshot = cast("dict[str, Any]", state["runtime_snapshot"])
            todos = normalize_todos(state.get("todos"))
            mode = normalize_mode(state.get("mode"))
            reading_viewport = state.get("reading_viewport")
            raw_capability_tools = state.get("capability_tools")
            if isinstance(raw_capability_tools, list):
                capability_tools = [str(item) for item in raw_capability_tools]
            capability_exclusive = bool(state.get("capability_exclusive", False))
            raw_persona_patterns = state.get("persona_tool_patterns")
            if isinstance(raw_persona_patterns, list):
                persona_tool_patterns = [str(item) for item in raw_persona_patterns]
            raw_workspace_files = state.get("workspace_files")
            if isinstance(raw_workspace_files, list):
                workspace_files = [str(item) for item in raw_workspace_files]
            for key, value in (
                ("environment_block", state.get("environment_block")),
                ("memory_block", state.get("memory_block")),
                ("persona_block", state.get("persona_block")),
                ("mode_block", state.get("mode_block")),
                ("locate_block", state.get("locate_block")),
                ("knowledge_block", state.get("knowledge_block")),
            ):
                if isinstance(value, str):
                    if key == "environment_block":
                        environment_block = value
                    elif key == "memory_block":
                        memory_block = value
                    elif key == "persona_block":
                        persona_block = value
                    elif key == "mode_block":
                        mode_block = value
                    elif key == "locate_block":
                        locate_block = value
                    else:
                        knowledge_block = value
    else:
        default_capabilities = build_work_capability_registry().resolve(
            CapabilityActivation(goal="", work_mode="office", persona_name="general")
        )
        capability_tools = sorted(default_capabilities.owned_tools)
        capability_exclusive = default_capabilities.exclusive
    if not canonical:
        from app.cowork_store.factory import local_cowork_stores

        local_messages = await local_cowork_stores().conversations.read(conversation_id)
        rows: list[dict[str, str]] | Any = [
            {"role": item.role, "content": item.content}
            for item in local_messages
            if item.status == "completed" and item.role in {"user", "assistant"} and item.content
        ]
        canonical = [{"role": str(row["role"]), "content": str(row["content"])} for row in rows]
        if rows:
            goal = str(rows[-1]["content"])

    session_roots = await list_session_roots(session, conversation_id=conversation_id)
    project_roots = tuple(Path(item.canonical_path) for item in session_roots)
    registry = _context_registry(settings, rag=rag, project_roots=project_roots)
    registry.restore_runtime_snapshot(runtime_snapshot)
    scope_state = cast(
        "Any",
        {
            "capability_tools": capability_tools,
            "capability_exclusive": capability_exclusive,
            "persona_tool_patterns": persona_tool_patterns,
        },
    )
    tools = registry.tool_definitions_for(
        goal,
        capability_tools=capability_tools,
    )
    scoped_allowed = _scoped_allowed_tools(scope_state, registry)
    if scoped_allowed is not None:
        tools = [item for item in tools if item.name in scoped_allowed]
    if mode == "plan":
        tools = registry.plan_mode_definitions(tools)
    # 与 runtime 的装配保持一致，否则页面上的占用条量的不是真正发出去的东西。
    deferred_tools_block = _deferred_tools_block(scope_state, registry)
    base_system_prompt = _system_prompt(
        registry.system_instructions(),
        environment_block=environment_block,
        memory_block=memory_block,
        persona_block=persona_block,
        mode_block=mode_block,
        locate_block=locate_block,
        knowledge_block=knowledge_block,
        workspace_files_block=render_workspace_files_block(workspace_files),
    )
    system_prompt = _system_prompt(
        registry.system_instructions(),
        environment_block=environment_block,
        memory_block=memory_block,
        persona_block=persona_block,
        mode_block=mode_block,
        deferred_tools_block=deferred_tools_block,
        locate_block=locate_block,
        knowledge_block=knowledge_block,
        workspace_files_block=render_workspace_files_block(workspace_files),
    )
    outbound = build_outbound_messages(
        canonical,
        compaction,
        system_prompt=system_prompt,
        prompts=COWORK_COMPACTION_PROMPTS,
        ephemeral_suffix=_ephemeral_context(
            mode=cast("CoworkMode", mode),
            todos=todos,
            roots_block=render_roots_block(session_roots),
            reading_viewport_block=render_reading_viewport_block(reading_viewport),
        ),
    )
    context_window = int(conversation["context_window"])
    budget = PromptBudget(
        task_type=COWORK_COMPACTION_PROMPTS.decision_task_type,
        tier="main",
        model=str(conversation["model"]),
        context_window_tokens=context_window,
        max_output_tokens=settings.cowork_decision_max_tokens,
        safety_tokens=settings.llm_context_safety_tokens,
    )
    total_tokens = budget.estimate_messages_tokens(outbound, tools)
    activated = registry.activated_tool_names()
    loaded_tools = [
        item for item in tools if item.name in activated and registry.get(item.name).deferred
    ]
    base_tools = [item for item in tools if item not in loaded_tools]
    tool_definition_tokens = _tool_definition_tokens(base_tools)
    loaded_tool_tokens = _tool_definition_tokens(loaded_tools)
    manifest_tokens = max(0, len(system_prompt) - len(base_system_prompt))
    system_tokens = (_message_tokens(outbound[0]) if outbound else 0) - manifest_tokens
    conversation_tokens = 0
    tool_activity_tokens = 0
    for message in outbound[1:]:
        cost = _message_tokens(message)
        if message.role == "tool" or message.tool_calls:
            tool_activity_tokens += cost
        else:
            conversation_tokens += cost
    accounted = (
        system_tokens
        + manifest_tokens
        + tool_definition_tokens
        + loaded_tool_tokens
        + conversation_tokens
        + tool_activity_tokens
    )
    system_tokens += max(0, total_tokens - accounted)
    trigger_tokens = max(
        1,
        int(budget.max_input_tokens * settings.cowork_compaction_trigger_ratio),
    )
    return {
        "used_tokens": total_tokens,
        "context_window_tokens": context_window,
        "max_input_tokens": budget.max_input_tokens,
        "trigger_tokens": trigger_tokens,
        "trigger_ratio": settings.cowork_compaction_trigger_ratio,
        "auto_compaction": settings.cowork_compaction_enabled,
        "compaction_revision": compaction["revision"],
        "compaction_mode": compaction["last_mode"],
        "model": str(conversation["model"]),
        "run_status": run_status,
        "estimated": True,
        "breakdown": {
            "system": system_tokens,
            "tool_manifest": manifest_tokens,
            "tools": tool_definition_tokens,
            "loaded_tools": loaded_tool_tokens,
            "messages": conversation_tokens,
            "tool_activity": tool_activity_tokens,
        },
    }
