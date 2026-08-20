"""Cowork 存储契约；RAG 的 PostgreSQL repository 不属于这个接口。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

from app.agent_core.contracts import InvocationLease, RunEvent, RunRecord, WorkflowType
from app.cowork_contracts import (
    AccessMode,
    ArtifactKind,
    ArtifactRecord,
    Capability,
    CapabilityGrantRecord,
    CoworkAttachmentRecord,
    InboxRecord,
    InteractionKind,
    PathAuthorization,
    ScheduleKind,
    ScheduleRecord,
    ScheduleView,
    SessionRootRecord,
    SteeringRecord,
)


@dataclass(frozen=True)
class StoredCheckpoint:
    run_id: UUID
    checkpoint_id: str
    parent_id: str | None
    state: dict[str, Any]


class CoworkStore(Protocol):
    """本地 Cowork 控制面所需的最小强一致接口。"""

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def create_conversation(self, *, title: str | None = None) -> UUID: ...

    async def conversation_exists(self, conversation_id: UUID) -> bool: ...

    async def allocate_message(
        self,
        *,
        record_id: UUID,
        conversation_id: UUID,
        role: str,
        status: str,
        run_id: UUID | None,
        title_source: str,
    ) -> int: ...

    async def update_message_status(self, *, record_id: UUID, status: str) -> None: ...

    async def get_message_conversation_id(self, *, record_id: UUID) -> UUID | None: ...

    async def list_conversation_metadata(
        self,
        *,
        conversation_id: UUID | None = None,
        archived: bool | None = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    async def set_conversation_archived(
        self, *, conversation_id: UUID, archived: bool
    ) -> bool: ...

    async def update_conversation_runtime(
        self,
        *,
        conversation_id: UUID,
        provider_profile_id: UUID | None,
        model_override: str | None,
        unattended: bool,
    ) -> bool: ...

    async def delete_conversation(self, *, conversation_id: UUID) -> bool: ...

    async def create_run(
        self,
        *,
        conversation_id: UUID,
        goal: str,
        budget_tokens: int,
        budget_calls: int,
        budget_wall_ms: int,
        answer_mode: Literal["grounded", "general"] = "general",
        retrieval_top_k: int = 5,
        workflow_type: WorkflowType = "cowork",
        schedule_id: UUID | None = None,
        unattended: bool = False,
        run_trigger: Literal["manual", "schedule", "catchup"] = "manual",
    ) -> RunRecord: ...

    async def get_run(self, run_id: UUID) -> RunRecord | None: ...

    async def get_latest_run(self, *, conversation_id: UUID) -> RunRecord | None: ...

    async def list_queued_runs(self, *, limit: int = 100) -> list[RunRecord]: ...

    async def conversation_has_active_run(self, *, conversation_id: UUID) -> bool: ...

    async def claim_run(
        self, *, run_id: UUID, worker_id: str, lease_s: int
    ) -> RunRecord | None: ...

    async def renew_run_lease(self, *, run_id: UUID, worker_id: str, lease_s: int) -> bool: ...

    async def append_events(
        self, *, run_id: UUID, events: Sequence[tuple[str, dict[str, Any]]]
    ) -> list[RunEvent]: ...

    async def list_events(self, *, run_id: UUID, after_seq: int = 0) -> list[RunEvent]: ...

    async def save_checkpoint(
        self,
        *,
        run_id: UUID,
        state: dict[str, Any],
        parent_id: str | None,
        checkpoint_id: str | None = None,
    ) -> StoredCheckpoint: ...

    async def load_latest_checkpoint(self, *, run_id: UUID) -> StoredCheckpoint | None: ...

    async def load_previous_checkpoint(self, *, run_id: UUID) -> StoredCheckpoint | None: ...

    async def acquire_invocation(
        self,
        *,
        run_id: UUID,
        plan_step_id: UUID,
        tool_name: str,
        args: dict[str, Any],
        worker_id: str,
        lease_s: int,
    ) -> InvocationLease: ...

    async def has_invocation(self, *, key: str) -> bool: ...

    async def complete_invocation(
        self,
        *,
        key: str,
        worker_id: str,
        result: dict[str, Any],
        effect_ref: str,
    ) -> None: ...

    async def fail_invocation(self, *, key: str, worker_id: str, error: str) -> None: ...

    async def claim_due_schedules(self, *, now_iso: str, limit: int = 50) -> list[UUID]: ...

    # permissions
    async def create_session_root(
        self,
        *,
        conversation_id: UUID,
        requested_path: str,
        access_mode: AccessMode,
        label: str | None = None,
    ) -> SessionRootRecord: ...

    async def list_session_roots(self, *, conversation_id: UUID) -> list[SessionRootRecord]: ...

    async def revoke_session_root(self, *, conversation_id: UUID, root_id: UUID) -> bool: ...

    async def grant_capability(
        self,
        *,
        conversation_id: UUID,
        capability: Capability,
        session_root_id: UUID | None = None,
        grant_source: Literal["user", "policy"] = "user",
        expires_in_s: int | None = None,
    ) -> CapabilityGrantRecord: ...

    async def list_capability_grants(
        self, *, conversation_id: UUID
    ) -> list[CapabilityGrantRecord]: ...

    async def revoke_capability_grant(self, *, conversation_id: UUID, grant_id: UUID) -> bool: ...

    async def authorize_capability(
        self, *, conversation_id: UUID, capability: Capability
    ) -> CapabilityGrantRecord: ...

    async def authorize_path(
        self, *, conversation_id: UUID, target_path: Path, capability: Capability
    ) -> PathAuthorization: ...

    # artifacts
    async def register_artifact(
        self,
        *,
        conversation_id: UUID,
        kind: ArtifactKind,
        title: str,
        uri: str,
        run_id: UUID | None = None,
        session_root_id: UUID | None = None,
        mime_type: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ArtifactRecord: ...

    async def list_artifacts(self, *, conversation_id: UUID) -> list[ArtifactRecord]: ...

    async def resolve_artifact_file(
        self, *, artifact_id: UUID
    ) -> tuple[ArtifactRecord, Path] | None: ...

    async def create_attachment(
        self,
        *,
        attachment_id: UUID,
        conversation_id: UUID,
        kind: str,
        filename: str,
        media_type: str,
        storage_path: str,
        size_bytes: int,
        sha256: str,
        extracted_text: str,
    ) -> CoworkAttachmentRecord: ...

    async def bind_attachments(
        self,
        *,
        conversation_id: UUID,
        attachment_ids: Sequence[UUID],
        message_id: UUID,
        run_id: UUID,
    ) -> list[CoworkAttachmentRecord]: ...

    async def list_run_attachments(self, *, run_id: UUID) -> list[CoworkAttachmentRecord]: ...

    # Inbox / steering
    async def enqueue_steering(
        self, *, run_id: UUID, conversation_id: UUID, content: str
    ) -> SteeringRecord: ...

    async def consume_pending_steering(self, *, run_id: UUID) -> list[SteeringRecord]: ...

    async def create_inbox_item(
        self,
        *,
        run_id: UUID,
        conversation_id: UUID,
        kind: InteractionKind,
        tool_call_id: str,
        plan_step_id: UUID,
        request: dict[str, Any],
    ) -> InboxRecord: ...

    async def get_inbox_item(self, *, run_id: UUID, resume_token: UUID) -> InboxRecord | None: ...

    async def list_unattended_inbox(
        self, *, include_resolved: bool = False, limit: int = 100
    ) -> list[Any]: ...

    async def update_inbox_item(
        self, *, item_id: UUID, status: str, response: dict[str, Any]
    ) -> InboxRecord | None: ...

    async def cancel_pending_interaction(self, *, run_id: UUID) -> None: ...

    # scheduler
    async def create_schedule(
        self,
        *,
        conversation_id: UUID,
        title: str,
        goal: str,
        schedule_kind: ScheduleKind,
        cron_expression: str | None,
        run_at: datetime | None,
        timezone: str,
        next_run_at: datetime,
    ) -> ScheduleRecord: ...

    async def get_schedule(self, *, schedule_id: UUID) -> ScheduleRecord | None: ...

    async def list_schedules(self, *, limit: int = 100) -> list[ScheduleView]: ...

    async def update_schedule_fields(
        self, *, schedule_id: UUID, values: dict[str, Any]
    ) -> ScheduleRecord | None: ...

    async def delete_schedule(self, *, schedule_id: UUID) -> bool: ...

    # runtime atomic primitives
    async def upsert_plan_step(
        self,
        *,
        step_id: UUID,
        run_id: UUID,
        step_idx: int,
        description: str,
        tool: str | None,
        status: str,
    ) -> None: ...

    async def update_plan_step_status(
        self, *, run_id: UUID, step_id: UUID, status: str
    ) -> None: ...

    async def next_attempt_no(self, *, run_id: UUID, plan_step_id: UUID, node: str) -> int: ...

    async def record_attempt(
        self,
        *,
        run_id: UUID,
        plan_step_id: UUID | None,
        attempt_no: int,
        node: str,
        status: str,
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
        tool_result: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        latency_ms: int | None = None,
        tokens: int | None = None,
        error_model: str | None = None,
    ) -> UUID: ...

    async def set_run_waiting_human(self, *, run_id: UUID, worker_id: str) -> bool: ...

    async def requeue_waiting_run(self, *, run_id: UUID) -> bool: ...

    async def add_run_usage(self, *, run_id: UUID, used_tokens: int, used_calls: int) -> None: ...

    async def finish_run(
        self,
        *,
        run_id: UUID,
        status: str,
        worker_id: str | None = None,
        error: str | None = None,
        used_tokens: int = 0,
        used_calls: int = 0,
    ) -> bool: ...

    async def request_cancel(self, *, run_id: UUID) -> RunRecord: ...

    async def reap_expired_runs(self, *, limit: int, max_recovery: int) -> dict[str, Any]: ...
