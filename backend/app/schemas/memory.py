from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

MemoryCategory = Literal["preference", "profile", "interest", "fact"]


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
