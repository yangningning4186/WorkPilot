from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

AccessMode = Literal["read_only", "read_write"]
Capability = Literal[
    "knowledge.read",
    "filesystem.read",
    "filesystem.write",
    "office.word.edit",
    "office.excel.edit",
    "network.read",
    "browser.control",
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


MemoryScope = Literal["global", "workspace", "conversation"]


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    scope: MemoryScope = "global"
    key: str | None = Field(default=None, min_length=1, max_length=120)


class MemoryPatch(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    # 撤销一次 forget。和 content 可以同时给：撤销一次"改写"就是还原旧文本。
    restore: bool = False

    @model_validator(mode="after")
    def _requires_a_change(self) -> "MemoryPatch":
        if self.content is None and not self.restore:
            raise ValueError("请提供要写入的内容，或设置 restore=true 恢复已 retire 的记忆")
        return self


class MemoryResponse(BaseModel):
    id: UUID
    scope: MemoryScope
    conversation_id: UUID | None
    workspace_path: str | None
    key: str | None
    content: str
    source: Literal["agent", "user"]
    created_at: datetime
    updated_at: datetime
    forgotten_at: datetime | None


class MemoryListResponse(BaseModel):
    items: list[MemoryResponse]


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


class AttachmentResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    message_id: UUID | None
    run_id: UUID | None
    kind: Literal["image", "pdf", "text"]
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
