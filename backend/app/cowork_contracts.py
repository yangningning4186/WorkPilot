"""Cowork 产品层与 Store adapter 共用的纯数据契约。

这里与 ``agent_core.contracts`` 的边界是：后者可被任何 Agent 复用；本模块只描述
Cowork 的目录授权、附件、交付物、HITL inbox 和自动化计划。两者都禁止依赖 service、
数据库实现或具体 Agent runtime。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

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
MemoryScope = Literal["global", "workspace", "conversation"]
ArtifactKind = Literal["file", "report", "diff", "table"]
AttachmentKind = Literal["image", "pdf", "text"]
InteractionKind = Literal[
    "ask_user",
    "directory_request",
    "capability_request",
    "shell_approval",
    "external_approval",
    "plan_approval",
]
InteractionStatus = Literal["pending", "answered", "approved", "rejected", "cancelled"]
ScheduleKind = Literal["once", "cron"]


class ConversationBusyError(RuntimeError):
    """会话仍有持有效租约的 worker，不能在其写入期间变更。"""


class CoworkPermissionError(RuntimeError):
    pass


class ConversationNotFoundError(LookupError):
    pass


class SessionRootNotFoundError(LookupError):
    pass


class CapabilityDeniedError(PermissionError):
    pass


class ArtifactRegistrationError(RuntimeError):
    pass


class CoworkAttachmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionRootRecord:
    id: UUID
    conversation_id: UUID
    requested_path: str
    canonical_path: str
    label: str
    access_mode: AccessMode
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CoworkMemoryRecord:
    """一条长期记忆。

    `forgotten_at` 是软删除：客户端的「撤销」和模型误删后的恢复都要能拿回原文，
    硬删掉就没有第二次机会了。真正的清理由用户在记忆面板里显式做。
    """

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


class MemoryNotFoundError(LookupError):
    pass


class MemoryScopeError(ValueError):
    pass


@dataclass(frozen=True)
class CapabilityGrantRecord:
    id: UUID
    conversation_id: UUID
    session_root_id: UUID | None
    capability: Capability
    grant_source: str
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def active(self) -> bool:
        return self.revoked_at is None and (
            self.expires_at is None or self.expires_at > datetime.now(UTC)
        )


@dataclass(frozen=True)
class PathAuthorization:
    conversation_id: UUID
    root_id: UUID
    root_path: Path
    target_path: Path
    access_mode: AccessMode
    capability: Capability


@dataclass(frozen=True)
class ArtifactRecord:
    id: UUID
    conversation_id: UUID
    run_id: UUID | None
    session_root_id: UUID | None
    kind: ArtifactKind
    title: str
    uri: str
    mime_type: str | None
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CoworkAttachmentRecord:
    id: UUID
    conversation_id: UUID
    message_id: UUID | None
    run_id: UUID | None
    kind: AttachmentKind
    filename: str
    media_type: str
    storage_path: str
    size_bytes: int
    sha256: str
    extracted_text: str


@dataclass(frozen=True)
class SteeringRecord:
    id: UUID
    run_id: UUID
    conversation_id: UUID
    content: str
    status: str
    created_at: datetime
    consumed_at: datetime | None


@dataclass(frozen=True)
class InboxRecord:
    id: UUID
    run_id: UUID
    conversation_id: UUID
    kind: InteractionKind
    status: InteractionStatus
    resume_token: UUID
    tool_call_id: str
    plan_step_id: UUID
    request: dict[str, Any]
    response: dict[str, Any] | None
    created_at: datetime
    responded_at: datetime | None
    unattended: bool


@dataclass(frozen=True)
class UnattendedInboxRecord:
    item: InboxRecord
    run_goal: str
    run_status: str
    schedule_id: UUID | None
    schedule_title: str | None


@dataclass(frozen=True)
class ScheduleRecord:
    id: UUID
    conversation_id: UUID
    title: str
    goal: str
    schedule_kind: ScheduleKind
    cron_expression: str | None
    run_at: datetime | None
    timezone: str
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_id: UUID | None
    run_count: int
    skipped_count: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ScheduleView:
    schedule: ScheduleRecord
    last_run_status: str | None
    pending_inbox_count: int
    workspace_label: str | None
    workspace_path: str | None
