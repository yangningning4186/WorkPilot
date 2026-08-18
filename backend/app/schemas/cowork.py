from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

AccessMode = Literal["read_only", "read_write"]
Capability = Literal[
    "filesystem.read",
    "filesystem.write",
    "office.word.edit",
    "office.excel.edit",
    "network.read",
    "shell.execute",
    "external.action",
]


class SessionRootCreate(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    access_mode: AccessMode
    label: str | None = Field(default=None, min_length=1, max_length=200)


class SessionRootResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    requested_path: str
    canonical_path: str
    label: str
    access_mode: AccessMode
    enabled: bool
    created_at: datetime
    updated_at: datetime


class SessionRootListResponse(BaseModel):
    items: list[SessionRootResponse]


class CapabilityGrantCreate(BaseModel):
    capability: Capability
    session_root_id: UUID | None = None
    expires_in_s: int | None = Field(default=None, ge=300, le=30 * 24 * 60 * 60)

    @model_validator(mode="after")
    def validate_scope(self) -> "CapabilityGrantCreate":
        path_capability = self.capability.startswith(("filesystem.", "office."))
        if path_capability != (self.session_root_id is not None):
            raise ValueError("文件能力必须绑定目录，网络/Shell/外部能力不能绑定目录")
        return self


class CapabilityGrantResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    session_root_id: UUID | None
    capability: Capability
    grant_source: str
    expires_at: datetime | None
    revoked_at: datetime | None
    active: bool
    created_at: datetime
    updated_at: datetime


class CapabilityGrantListResponse(BaseModel):
    items: list[CapabilityGrantResponse]


class ArtifactResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    run_id: UUID | None
    session_root_id: UUID | None
    kind: Literal["file", "report", "diff", "table"]
    title: str
    uri: str
    mime_type: str | None
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ArtifactListResponse(BaseModel):
    items: list[ArtifactResponse]
