from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class ScheduleCreate(BaseModel):
    conversation_id: UUID
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=4000)
    schedule_kind: Literal["once", "cron"]
    cron_expression: str | None = Field(default=None, max_length=200)
    run_at: datetime | None = None
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_shape(self) -> "ScheduleCreate":
        if self.schedule_kind == "once" and (self.run_at is None or self.cron_expression is not None):
            raise ValueError("单次计划必须只填写 run_at")
        if self.schedule_kind == "cron" and (not self.cron_expression or self.run_at is not None):
            raise ValueError("周期计划必须只填写 cron_expression")
        return self


class ScheduleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    goal: str | None = Field(default=None, min_length=1, max_length=4000)
    enabled: bool | None = None
    cron_expression: str | None = Field(default=None, max_length=200)
    run_at: datetime | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=100)


class ScheduleResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    title: str
    goal: str
    schedule_kind: Literal["once", "cron"]
    cron_expression: str | None
    run_at: datetime | None
    timezone: str
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_id: UUID | None
    last_run_status: str | None
    run_count: int
    skipped_count: int
    pending_inbox_count: int
    created_at: datetime
    updated_at: datetime


class ScheduleListResponse(BaseModel):
    items: list[ScheduleResponse]
    total: int


class UnattendedInboxItemResponse(BaseModel):
    id: UUID
    run_id: UUID
    conversation_id: UUID
    schedule_id: UUID | None
    schedule_title: str | None
    run_goal: str
    run_status: str
    kind: Literal[
        "ask_user",
        "directory_request",
        "capability_request",
        "shell_approval",
        "external_approval",
    ]
    status: Literal["pending", "answered", "approved", "rejected", "cancelled"]
    resume_token: UUID
    request: dict[str, Any]
    response: dict[str, Any] | None
    created_at: datetime
    responded_at: datetime | None


class UnattendedInboxListResponse(BaseModel):
    items: list[UnattendedInboxItemResponse]
    total: int
