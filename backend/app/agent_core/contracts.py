"""Agent 运行时跨层共享的纯数据契约。

本模块禁止依赖 ``app.agent``、``app.services``、数据库或具体 Provider，Store、API、
worker 与运行时都只能从这里引用共享 DTO。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypedDict
from uuid import UUID

WorkflowType = Literal["answer", "literature_review", "cowork"]
RunTrigger = Literal["manual", "schedule", "catchup"]
AnswerMode = Literal["grounded", "general"]
TERMINAL_RUN_STATUSES = frozenset({"done", "failed", "cancelled", "budget_exceeded"})


class BudgetState(TypedDict):
    max_tokens: int
    used_tokens: int
    max_calls: int
    used_calls: int
    max_wall_ms: int
    used_wall_ms: int
    started_at_ms: int


@dataclass(frozen=True)
class RunEvent:
    run_id: UUID
    seq: int
    type: str
    payload: dict[str, Any]
    created_at: datetime

    @property
    def event_id(self) -> str:
        return f"{self.run_id}:{self.seq}"

    def envelope(self) -> dict[str, Any]:
        # seq 用字符串：数据库是 BIGINT，直接传 JS number 会丢精度。
        return {
            "id": self.event_id,
            "run_id": str(self.run_id),
            "seq": str(self.seq),
            "type": self.type,
            "data": self.payload,
        }


@dataclass(frozen=True)
class RunRecord:
    id: UUID
    conversation_id: UUID
    goal: str
    status: str
    worker_id: str | None
    lease_until: datetime | None
    cancel_requested_at: datetime | None
    budget_tokens: int
    budget_calls: int
    budget_wall_ms: int
    used_tokens: int
    used_calls: int
    next_seq: int
    error: str | None
    schedule_id: UUID | None
    unattended: bool
    run_trigger: RunTrigger
    workflow_type: WorkflowType = "answer"
    answer_mode: AnswerMode = "grounded"
    retrieval_top_k: int = 5

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES

    @property
    def cancel_requested(self) -> bool:
        return self.cancel_requested_at is not None


@dataclass(frozen=True)
class InvocationLease:
    idempotency_key: str
    acquired: bool
    result: dict[str, Any] | None = None
    effect_ref: str | None = None


class HumanInterrupt(TypedDict):
    inbox_id: str
    kind: str
    resume_token: str
    tool_call_id: str
    step_id: str
    step_idx: int
    request: dict[str, Any]
