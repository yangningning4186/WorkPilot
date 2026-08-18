from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ConversationCreate(BaseModel):
    title: str = Field(default="新会话", min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("会话标题不能为空")
        return normalized


class ConversationResponse(BaseModel):
    id: UUID
    title: str | None
    message_count: int
    latest_message: str | None
    last_message_at: datetime | None
    provider_profile_id: UUID | None
    provider_name: str | None
    provider: str | None
    selected_model: str | None
    unattended: bool
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    items: list[ConversationResponse]
    total: int


class ConversationRuntimeUpdate(BaseModel):
    provider_profile_id: UUID | None = None
    model_override: str | None = Field(default=None, max_length=200)
    unattended: bool = False

    @field_validator("model_override")
    @classmethod
    def normalize_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ConversationMessageResponse(BaseModel):
    id: UUID
    seq: int
    role: Literal["user", "assistant"]
    content: str
    status: str
    run_id: UUID | None
    citations: list[dict[str, Any]]
    answer_mode: Literal["grounded", "general"] | None
    created_at: datetime


class ConversationMessageListResponse(BaseModel):
    items: list[ConversationMessageResponse]
    total: int
