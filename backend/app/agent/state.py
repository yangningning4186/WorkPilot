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
    # 累计"执行中"墙钟。只累加执行分段的时长, 因此 waiting_human 的人工思考时间与
    # 两次 worker 之间的空档都不计入——墙钟预算防的是失控执行, 不是人的犹豫。
    used_wall_ms: int
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


def normalize_budget(state: AgentState) -> AgentState:
    """给早于预算熔断的 checkpoint 补上缺失的预算字段。

    `used_wall_ms` 是在 A0 骨架之后才加的。老 checkpoint 反序列化回来会缺这个键,
    TypedDict 不做运行时校验, 缺键要到第一次累加时才炸。这里显式补 0:
    老 run 的已耗墙钟无法追认, 按 0 起算是唯一诚实的选择, 且只会让预算更宽松而不是更严。
    """

    budget = state["budget"]
    if "used_wall_ms" not in budget:
        budget["used_wall_ms"] = 0
    return state


def json_state(state: AgentState) -> AgentState:
    """拒绝 UUID/datetime/客户端等不可 JSON 化对象，并返回无别名的纯数据副本。"""

    encoded = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # pragma: no cover - AgentState 根节点固定为对象
        raise TypeError("AgentState 必须是 JSON object")
    return cast("AgentState", decoded)

