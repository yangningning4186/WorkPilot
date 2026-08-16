"""固定综述工作流的显式、可 JSON 序列化状态。"""

from __future__ import annotations

import json
from typing import Literal, TypedDict, cast


class PlanStepState(TypedDict):
    id: str
    idx: int
    description: str
    tool: str | None
    depends_on: list[int]
    status: Literal["pending", "running", "done", "failed", "skipped"]


class BudgetState(TypedDict):
    max_tokens: int
    used_tokens: int
    max_calls: int
    used_calls: int
    max_wall_ms: int
    started_at_ms: int


class InterruptState(TypedDict):
    kind: Literal["write_confirm"]
    payload: dict[str, object]
    resume_token: str


class ReviewDocument(TypedDict):
    document_id: str
    version_id: str
    title: str
    source_uri: str


class ReviewCard(TypedDict):
    document_id: str
    title: str
    core_problem: str
    method_family: str
    method: str
    findings: list[str]
    limitations: list[str]
    evidence_quotes: list[str]


class ReviewGroup(TypedDict):
    name: str
    document_ids: list[str]


class AgentState(TypedDict):
    schema_version: Literal["literature-review.v1"]
    run_id: str
    conversation_id: str
    goal: str
    document_ids: list[str]
    plan: list[PlanStepState]
    cursor: int
    documents: list[ReviewDocument]
    cards: list[ReviewCard]
    groups: list[ReviewGroup]
    comparison: str
    draft: str
    output_path: str | None
    artifacts: dict[str, str]
    budget: BudgetState
    interrupt: InterruptState | None
    status: Literal[
        "executing",
        "waiting_human",
        "done",
        "failed",
        "cancelled",
        "budget_exceeded",
    ]
    error: str | None


def json_state(state: AgentState) -> AgentState:
    """拒绝 UUID/datetime/客户端等不可 JSON 化对象，并返回无别名的纯数据副本。"""

    encoded = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # pragma: no cover - AgentState 根节点固定为对象
        raise TypeError("AgentState 必须是 JSON object")
    return cast("AgentState", decoded)

