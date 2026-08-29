"""Cowork RunConfig 的中立持久化投影；不得反向依赖产品 runtime。"""

from __future__ import annotations

from typing import Any

RUN_CONFIG_KEYS: tuple[str, ...] = (
    "run_id",
    "conversation_id",
    "goal",
    "environment_block",
    "standing_rules_block",
    "memory_block",
    "session_facts",
    "work_mode",
    "active_capabilities",
    "capability_tools",
    "capability_exclusive",
    "persona_name",
    "persona_snapshot",
    "persona_block",
    "persona_tool_patterns",
    "mode_block",
    "workspace_files",
    "reading_path",
    "locate_block",
    "kb_slug",
    "knowledge_block",
)


def split_cowork_state(
    state: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Known Cowork states split into one config row and a mutable checkpoint."""

    if state.get("schema_version") not in {"cowork.v2", "cowork.v3"}:
        return None, dict(state)
    config = {key: state[key] for key in RUN_CONFIG_KEYS if key in state}
    checkpoint = {key: value for key, value in state.items() if key not in RUN_CONFIG_KEYS}
    return config, checkpoint


def merge_cowork_state(
    checkpoint: dict[str, Any], run_config: dict[str, Any] | None
) -> dict[str, Any]:
    """读取时重建运行时视图；v22 以前的整份 checkpoint 无 config 也可恢复。"""

    if run_config is None:
        return checkpoint
    return {**run_config, **checkpoint}
