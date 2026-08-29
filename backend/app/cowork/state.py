"""Cowork run 的冻结配置、可变 checkpoint 与运行时合并视图。"""

from __future__ import annotations

import json
from typing import Any, Literal, TypedDict, cast

from app.agent_core.compaction import CompactionState
from app.agent_core.contracts import BudgetState, HumanInterrupt
from app.agent_core.messages import AgentMessage
from app.cowork.evidence import EvidenceRecord
from app.cowork.personas import PersonaSnapshot
from app.cowork.plans import CoworkMode
from app.cowork.session_facts import SessionFacts
from app.cowork.todos import TodoItem
from app.cowork_contracts import CoworkWorkMode
from app.cowork_store.run_config import RUN_CONFIG_KEYS


class PendingToolCall(TypedDict):
    call_id: str
    name: str
    arguments: str
    step_idx: int
    step_id: str


class CoworkRunConfig(TypedDict):
    """一次 run 内冻结、且不应在每个 checkpoint 重复序列化的输入。"""

    run_id: str
    conversation_id: str
    goal: str
    environment_block: str
    standing_rules_block: str
    memory_block: str
    session_facts: SessionFacts
    work_mode: CoworkWorkMode
    active_capabilities: list[str]
    capability_tools: list[str]
    capability_exclusive: bool
    persona_name: str
    persona_snapshot: PersonaSnapshot | None
    persona_block: str
    persona_tool_patterns: list[str]
    mode_block: str
    workspace_files: list[str]
    reading_path: str | None
    locate_block: str
    kb_slug: str | None
    knowledge_block: str


class CoworkCheckpointState(TypedDict):
    """每轮真正发生变化、因此需要 append-only checkpoint 的状态。"""

    schema_version: Literal["cowork.v3"]
    messages: list[AgentMessage]
    iteration: int
    pending_calls: list[PendingToolCall]
    approved_calls: list[str]
    approval_evidence: dict[str, dict[str, Any]]
    semantic_approval_signing_key: str
    semantic_review_consecutive_denies: int
    semantic_review_breaker_tripped: bool
    semantic_review_breaker_persisted: bool
    semantic_review_user_text_source: Literal["local_owner", "external_inbound", "unknown"]
    interrupt: HumanInterrupt | None
    compaction: CompactionState
    final_message: str
    status: Literal[
        "executing",
        "waiting_human",
        "sleeping",
        "done",
        "failed",
        "cancelled",
        "budget_exceeded",
        "provider_retry",
    ]
    error: str | None
    budget: BudgetState
    runtime_snapshot: dict[str, Any]
    history_loaded: bool
    todos: list[TodoItem]
    mode: CoworkMode
    skill_countermand_block: str
    reading_viewport: dict[str, Any] | None
    call_signatures: dict[str, int]
    stalled_rounds: int
    evidence_ledger: list[EvidenceRecord]
    citation_repair_attempts: int
    model_truncation_retries: int
    last_turn_span_id: str | None
    final_citations: list[dict[str, Any]]


class CoworkState(CoworkRunConfig, CoworkCheckpointState):
    """只在内存中使用的完整视图；Store 读写边界负责 split / merge。"""


if CoworkRunConfig.__required_keys__ != frozenset(RUN_CONFIG_KEYS):  # pragma: no cover
    raise RuntimeError("CoworkRunConfig 与持久化字段集合不一致")


def json_cowork_state(state: CoworkState) -> CoworkState:
    encoded = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    value = json.loads(encoded)
    if not isinstance(value, dict):  # pragma: no cover
        raise TypeError("Cowork state 必须是 JSON object")
    return cast("CoworkState", value)


def cowork_run_config(state: CoworkState) -> CoworkRunConfig:
    """复制冻结字段，供首轮 pre-loop 与下一次 checkpoint 原子定稿。"""

    raw = cast("dict[str, Any]", state)
    return cast("CoworkRunConfig", {key: raw[key] for key in RUN_CONFIG_KEYS})
