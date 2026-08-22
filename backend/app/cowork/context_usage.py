"""Cowork 当前 outbound 上下文的只读估算。"""

from __future__ import annotations

import json
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
from app.cowork.connector_tools import register_connector_tools
from app.cowork.environment import render_roots_block
from app.cowork.extensions import register_skill_tools
from app.cowork.memory_tools import register_memory_tools
from app.cowork.permissions import list_session_roots
from app.cowork.plans import CoworkMode, normalize_mode
from app.cowork.provider_profiles import get_provider_profile
from app.cowork.runtime import (
    COWORK_COMPACTION_PROMPTS,
    _ephemeral_context,
    _system_prompt,
    _tools_referenced_in_history,
)
from app.cowork.subagent import register_readonly_subagent
from app.cowork.todos import TodoItem, normalize_todos
from app.cowork.tools import CoworkToolRegistry, build_default_cowork_registry
from app.cowork_store.routing import cowork_store
from workpilot_ai.gateway import PromptBudget, request_character_count
from workpilot_ai.types import Message, ToolDefinition

_CONTEXT_REGISTRY_CACHE: tuple[tuple[Any, ...], CoworkToolRegistry] | None = None


def _context_registry(settings: Settings) -> CoworkToolRegistry:
    """缓存只读上下文估算使用的工具 schema；Skill 文件变化时自动失效。"""

    global _CONTEXT_REGISTRY_CACHE
    skills_root = settings.cowork_skills_path.expanduser()
    skill_revision = tuple(
        sorted(
            (str(path), path.stat().st_mtime_ns, path.stat().st_size)
            for path in skills_root.glob("*/SKILL.md")
            if path.is_file()
        )
    )
    key = (
        str(skills_root),
        settings.cowork_skill_max_files,
        settings.cowork_skill_max_bytes,
        skill_revision,
    )
    if _CONTEXT_REGISTRY_CACHE is not None and _CONTEXT_REGISTRY_CACHE[0] == key:
        return _CONTEXT_REGISTRY_CACHE[1]
    registry = build_default_cowork_registry()
    register_skill_tools(registry, settings)
    register_browser_tools(registry)
    register_connector_tools(registry)
    register_scheduler_tools(registry)
    register_memory_tools(registry)
    register_readonly_subagent(registry)
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
    # Profile 出了数据库，这里少了一条 LEFT JOIN：id 悬空（用户删了 profile）时
    # 按"没选 Provider"处理，回落到默认档位的上下文窗口。
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
        or "系统默认",
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
    mode = "execute"
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
            for key, value in (
                ("environment_block", state.get("environment_block")),
                ("memory_block", state.get("memory_block")),
            ):
                if isinstance(value, str):
                    if key == "environment_block":
                        environment_block = value
                    else:
                        memory_block = value
    if not canonical:
        from app.cowork_store.factory import local_cowork_stores

        local_messages = await local_cowork_stores().conversations.read(conversation_id)
        rows: list[dict[str, str]] | Any = [
            {"role": item.role, "content": item.content}
            for item in local_messages
            if item.status == "completed"
            and item.role in {"user", "assistant"}
            and item.content
        ]
        canonical = [{"role": str(row["role"]), "content": str(row["content"])} for row in rows]
        if rows:
            goal = str(rows[-1]["content"])

    registry = _context_registry(settings)
    tools = registry.tool_definitions_for(
        goal,
        retained_tools=(
            registry.activated_tools_from_snapshot(runtime_snapshot)
            | _tools_referenced_in_history(canonical)
        ),
    )
    # 与 runtime 的装配保持一致，否则页面上的占用条量的不是真正发出去的东西。
    system_prompt = _system_prompt(
        registry.system_instructions(),
        environment_block=environment_block,
        memory_block=memory_block,
    )
    outbound = build_outbound_messages(
        canonical,
        compaction,
        system_prompt=system_prompt,
        prompts=COWORK_COMPACTION_PROMPTS,
        ephemeral_suffix=_ephemeral_context(
            mode=cast("CoworkMode", mode),
            todos=todos,
            roots_block=render_roots_block(
                await list_session_roots(session, conversation_id=conversation_id)
            ),
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
    tool_definition_tokens = _tool_definition_tokens(tools)
    system_tokens = _message_tokens(outbound[0]) if outbound else 0
    conversation_tokens = 0
    tool_activity_tokens = 0
    for message in outbound[1:]:
        cost = _message_tokens(message)
        if message.role == "tool" or message.tool_calls:
            tool_activity_tokens += cost
        else:
            conversation_tokens += cost
    accounted = system_tokens + tool_definition_tokens + conversation_tokens + tool_activity_tokens
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
            "tools": tool_definition_tokens,
            "messages": conversation_tokens,
            "tool_activity": tool_activity_tokens,
        },
    }
