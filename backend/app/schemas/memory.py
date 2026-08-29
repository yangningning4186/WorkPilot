from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.cowork_contracts import MAX_STANDING_RULES_CHARS

MemoryCategory = Literal["preference", "profile", "interest", "fact"]
MemoryPolicyMode = Literal["inherit", "on", "off"]


class OwnerMemoryPolicyUpdate(BaseModel):
    expected_revision: int = Field(ge=0)
    save_enabled: bool | None = None
    recall_enabled: bool | None = None
    standing_rules: str | None = Field(default=None, max_length=MAX_STANDING_RULES_CHARS)

    @model_validator(mode="after")
    def require_change(self) -> "OwnerMemoryPolicyUpdate":
        if (
            self.save_enabled is None
            and self.recall_enabled is None
            and self.standing_rules is None
        ):
            raise ValueError("至少提供一个要修改的记忆策略字段")
        return self


class OwnerMemoryPolicyResponse(BaseModel):
    revision: int
    save_enabled: bool
    recall_enabled: bool
    standing_rules: str
    deployment_save_enabled: bool
    deployment_recall_enabled: bool
    effective_save_enabled: bool
    effective_recall_enabled: bool
    save_disabled_reason: str | None
    recall_disabled_reason: str | None


class ConversationMemoryPolicyUpdate(BaseModel):
    expected_revision: int = Field(ge=0)
    save_mode: MemoryPolicyMode | None = None
    recall_mode: MemoryPolicyMode | None = None

    @model_validator(mode="after")
    def require_change(self) -> "ConversationMemoryPolicyUpdate":
        if self.save_mode is None and self.recall_mode is None:
            raise ValueError("至少提供一个要修改的会话记忆策略字段")
        return self


class ConversationMemoryPolicyResponse(BaseModel):
    conversation_id: UUID
    revision: int
    save_mode: MemoryPolicyMode
    recall_mode: MemoryPolicyMode
    effective_save_enabled: bool
    effective_recall_enabled: bool
    save_disabled_reason: str | None
    recall_disabled_reason: str | None


class MemoryResponse(BaseModel):
    id: UUID
    category: MemoryCategory
    fact: str
    valid_from: datetime
    invalid_at: datetime | None
    superseded_by: UUID | None
    source_type: Literal["conversation", "manual"]
    source_message_id: UUID | None
    confidence: float
    access_count: int
    last_used_at: datetime | None
    pinned: bool
    created_at: datetime
    updated_at: datetime


class MemoryListResponse(BaseModel):
    items: list[MemoryResponse]
    total: int


class MemoryCreate(BaseModel):
    category: MemoryCategory
    fact: str = Field(min_length=1, max_length=2000)
    pinned: bool = False


class MemoryUpdate(BaseModel):
    category: MemoryCategory | None = None
    fact: str | None = Field(default=None, min_length=1, max_length=2000)
    pinned: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> "MemoryUpdate":
        if self.category is None and self.fact is None and self.pinned is None:
            raise ValueError("至少提供一个要修改的字段")
        return self
