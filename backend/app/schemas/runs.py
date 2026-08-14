from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# 与 docs/08 §3.2 的 EventType 对齐。plan / step.update / interrupt 随 M1 的
# LangGraph 工作流一起引入, 现在发不出来就先不进契约。
RunEventType = Literal[
    "message.start",
    "message.delta",
    "citation",
    "message.done",
    "error",
]


AnswerMode = Literal["grounded", "general"]


class CreateRunRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None
    top_k: int = Field(default=5, ge=1, le=20)
    # general 只能由用户在拒答之后显式选择, 默认永远是可溯源的资料库回答。
    mode: AnswerMode = "grounded"


class CreateRunResponse(BaseModel):
    run_id: UUID
    conversation_id: UUID
    status: str


class RunStatusResponse(BaseModel):
    run_id: UUID
    conversation_id: UUID
    goal: str
    answer_mode: AnswerMode
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
