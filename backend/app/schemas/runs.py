from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# 与 docs/08 §3.2 的 EventType 对齐。
RunEventType = Literal[
    "message.start",
    "message.delta",
    "citation",
    "message.done",
    "plan",
    "step.update",
    "interrupt",
    "artifact",
    "run.done",
    "error",
]


AnswerMode = Literal["grounded", "general"]
WorkflowType = Literal["answer", "literature_review"]


class CreateRunRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None
    top_k: int = Field(default=5, ge=1, le=20)
    # general 只能由用户在拒答之后显式选择, 默认永远是可溯源的资料库回答。
    mode: AnswerMode = "grounded"


class CreateReviewRunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)
    document_ids: list[UUID] = Field(min_length=2, max_length=50)
    output_path: str = Field(min_length=4, max_length=500)
    conversation_id: UUID | None = None


class ResumeRunRequest(BaseModel):
    resume_token: str = Field(min_length=1, max_length=200)
    approved: bool


class CreateRunResponse(BaseModel):
    run_id: UUID
    conversation_id: UUID
    status: str
    workflow_type: WorkflowType


class RunStatusResponse(BaseModel):
    run_id: UUID
    conversation_id: UUID
    goal: str
    answer_mode: AnswerMode
    workflow_type: WorkflowType
    status: str
    cancel_requested: bool
    used_tokens: int
    used_calls: int
    next_seq: int
    error: str | None


class RunEventEnvelope(BaseModel):
    """SSE `data:` 里的信封, 与前端 StreamEnvelope 一一对应。"""

    id: str
    run_id: UUID
    # seq 是 BIGINT, 用字符串传避免 JS number 精度丢失; 前端比较时转 BigInt。
    seq: str
    type: str
    data: dict[str, Any]
