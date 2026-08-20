"""固定综述工作流的显式、可 JSON 序列化状态（产品层，见 ADR-0011）。

通用的计划步骤、人工中断与序列化在 `app/agent_core/state.py`；这里只有综述自己的
文档、卡片、分组与成稿字段。原名 `ReviewState`——它从来就不是"Agent 通用状态"，
`schema_version` 写的就是 `literature-review.v1`。
"""

from __future__ import annotations

from typing import Literal, TypedDict

from app.agent_core.contracts import BudgetState as BudgetState
from app.agent_core.state import InterruptState as InterruptState
from app.agent_core.state import PlanStepState as PlanStepState
from app.agent_core.state import json_state as json_state
from app.agent_core.state import normalize_budget as normalize_budget


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


class ReviewState(TypedDict):
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
