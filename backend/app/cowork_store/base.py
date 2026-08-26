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
    AnnotationColor,
    ApprovalMatchKind,
    ApprovalMode,
    ApprovalRuleRecord,
    ApprovalRuleScope,
    ArtifactKind,
    ArtifactRecord,
    BoardTaskRecord,
    Capability,
    CapabilityGrantRecord,
    ChannelSubscriptionRecord,
    CoworkAttachmentRecord,
    CoworkMemoryRecord,
    InboxBindingRecord,
    InboxRecord,
    InteractionKind,
    MemoryCategory,
    MemoryExtractionJob,
    MemoryScope,
    PathAuthorization,
    ReadingAnnotationRecord,
    ScheduleKind,
    ScheduleRecord,
    ScheduleView,
    SessionRootRecord,
    SteeringRecord,
    TeamRecord,
    TeamWorkerRecord,
    TeamWorkerSessionRecord,
    ThreadSessionRecord,
    UnroutedRecord,
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

    async def compare_and_set_conversation_title(
        self,
        *,
        conversation_id: UUID,
        expected_title: str | None,
        title: str,
    ) -> bool: ...

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

    async def list_streaming_message_ids(self, *, run_id: UUID) -> list[UUID]: ...

    async def update_message_status(
        self,
        *,
        record_id: UUID,
        status: str,
        content_preview: str | None = None,
    ) -> None: ...

    async def get_message_conversation_id(self, *, record_id: UUID) -> UUID | None: ...

    async def list_conversation_metadata(
        self,
        *,
        conversation_id: UUID | None = None,
        archived: bool | None = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    async def set_conversation_archived(self, *, conversation_id: UUID, archived: bool) -> bool: ...

    async def update_conversation_runtime(
        self,
        *,
        conversation_id: UUID,
        provider_profile_id: UUID | None,
        model_override: str | None,
        unattended: bool,
        approval_mode: ApprovalMode,
        persona_name: str,
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
        initializing: bool = False,
    ) -> RunRecord: ...

    async def initialize_run(
        self,
        *,
        run_id: UUID,
        state: dict[str, Any],
        checkpoint_id: str,
        events: Sequence[tuple[str, dict[str, Any]]],
    ) -> tuple[RunRecord, StoredCheckpoint, list[RunEvent]]: ...

    async def get_run(self, run_id: UUID) -> RunRecord | None: ...

    async def get_runs(self, run_ids: Sequence[UUID]) -> list[RunRecord]: ...

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

    async def list_events(
        self,
        *,
        run_id: UUID,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[RunEvent]: ...

    async def save_checkpoint(
        self,
        *,
        run_id: UUID,
        state: dict[str, Any],
        parent_id: str | None,
        checkpoint_id: str | None = None,
    ) -> StoredCheckpoint: ...

    async def commit_checkpoint(
        self,
        *,
        run_id: UUID,
        state: dict[str, Any],
        parent_id: str | None,
        checkpoint_id: str,
        used_tokens: int,
        used_calls: int,
        events: Sequence[tuple[str, dict[str, Any]]],
        worker_id: str | None = None,
        transition_to: Literal["queued", "waiting_human", "sleeping"] | None = None,
        wake_at: datetime | None = None,
    ) -> tuple[StoredCheckpoint, list[RunEvent]]: ...

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
        resource_scope: str | None = None,
        grant_source: Literal["user", "policy"] = "user",
        expires_in_s: int | None = None,
    ) -> CapabilityGrantRecord: ...

    async def list_capability_grants(
        self, *, conversation_id: UUID
    ) -> list[CapabilityGrantRecord]: ...

    async def revoke_capability_grant(self, *, conversation_id: UUID, grant_id: UUID) -> bool: ...

    async def upsert_inbox_binding(
        self,
        *,
        name: str,
        platform: str | None,
        chat_id: str | None,
        connector_account_id: UUID | None,
        enabled: bool,
    ) -> InboxBindingRecord: ...

    async def get_inbox_binding(self, *, name: str) -> InboxBindingRecord | None: ...

    async def list_inbox_bindings(self) -> list[InboxBindingRecord]: ...

    async def delete_inbox_binding(self, *, name: str) -> bool: ...

    async def set_conversation_inbox(
        self, *, conversation_id: UUID, inbox_name: str | None
    ) -> bool: ...

    async def get_conversation_inbox(self, *, conversation_id: UUID) -> str | None: ...

    async def set_conversation_kb(self, *, conversation_id: UUID, kb_slug: str | None) -> bool: ...

    async def get_conversation_kb(self, *, conversation_id: UUID) -> str | None: ...

    async def set_inbox_delivery_ref(self, *, item_id: UUID, delivery_ref: str) -> None: ...

    async def get_inbox_item_by_id(self, *, item_id: UUID) -> InboxRecord | None: ...

    async def create_channel_subscription(
        self,
        *,
        conversation_id: UUID,
        platform: str,
        chat_id: str,
        connector_account_id: UUID | None,
    ) -> ChannelSubscriptionRecord: ...

    async def list_channel_subscriptions(
        self, *, conversation_id: UUID | None = None, channel: tuple[str, str] | None = None
    ) -> list[ChannelSubscriptionRecord]: ...

    async def revoke_channel_subscription(
        self, *, conversation_id: UUID, subscription_id: UUID
    ) -> bool: ...

    async def upsert_thread_session(
        self,
        *,
        target: str,
        conversation_id: UUID,
        platform: str,
        chat_id: str,
        thread_id: str,
    ) -> ThreadSessionRecord: ...

    async def get_thread_session(self, *, target: str) -> ThreadSessionRecord | None: ...

    async def list_thread_sessions(self, *, conversation_id: UUID) -> list[ThreadSessionRecord]: ...

    async def record_unrouted(
        self,
        *,
        kind: str,
        platform: str | None,
        chat_id: str | None,
        summary: str,
        payload: dict[str, Any],
        keep: int,
    ) -> UnroutedRecord: ...

    async def list_unrouted(self, *, limit: int) -> list[UnroutedRecord]: ...

    async def create_reading_annotation(
        self,
        *,
        material_id: str,
        path: str,
        locator: int,
        quote: str,
        note: str,
        color: AnnotationColor,
        locations: Sequence[dict[str, Any]],
        conversation_id: UUID | None,
        run_id: UUID | None,
        max_per_material: int,
    ) -> ReadingAnnotationRecord: ...

    async def list_reading_annotations(
        self, *, material_id: str
    ) -> list[ReadingAnnotationRecord]: ...

    async def count_stale_reading_annotations(self, *, path: str, material_id: str) -> int: ...

    async def delete_reading_annotation(self, *, annotation_id: UUID) -> bool: ...

    async def set_workspace_trust(self, *, canonical_path: str, trusted: bool) -> bool: ...

    async def is_workspace_trusted(self, *, canonical_path: str) -> bool: ...

    async def list_workspace_trust(self) -> list[str]: ...

    async def create_approval_rule(
        self,
        *,
        conversation_id: UUID,
        tool: str,
        match_kind: ApprovalMatchKind,
        target: str | None,
        scope: ApprovalRuleScope,
        schedule_id: UUID | None,
        created_by: str,
    ) -> ApprovalRuleRecord: ...

    async def list_approval_rules(
        self, *, conversation_id: UUID, include_revoked: bool = False
    ) -> list[ApprovalRuleRecord]: ...

    async def revoke_approval_rule(self, *, conversation_id: UUID, rule_id: UUID) -> bool: ...

    async def authorize_capability(
        self, *, conversation_id: UUID, capability: Capability
    ) -> CapabilityGrantRecord: ...

    async def authorize_scoped_capability(
        self, *, conversation_id: UUID, capability: Capability, target: str
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

    async def list_run_artifacts(self, *, run_id: UUID) -> list[ArtifactRecord]: ...

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

    # Agent Teams / Board
    async def create_team(
        self,
        *,
        lead_conversation_id: UUID,
        proposal_call_id: str,
        note: str,
        members: Sequence[dict[str, Any]],
    ) -> tuple[TeamRecord, list[TeamWorkerRecord]]: ...

    async def get_team_for_lead(self, *, lead_conversation_id: UUID) -> TeamRecord | None: ...

    async def list_team_workers(self, *, team_id: UUID) -> list[TeamWorkerRecord]: ...

    async def create_board_task(
        self,
        *,
        lead_conversation_id: UUID,
        title: str,
        description: str,
        acceptance_criteria: str,
        resource_scope: Sequence[dict[str, str]],
    ) -> BoardTaskRecord: ...

    async def list_board_tasks(
        self,
        *,
        lead_conversation_id: UUID,
        status: str | None = None,
        assignee: str | None = None,
    ) -> list[BoardTaskRecord]: ...

    async def start_board_task(
        self,
        *,
        lead_conversation_id: UUID,
        task_id: UUID,
        worker_name: str,
        assignment_call_id: str,
    ) -> tuple[BoardTaskRecord, TeamWorkerRecord, TeamWorkerSessionRecord]: ...

    async def save_team_worker_session(
        self,
        *,
        session_id: UUID,
        task_id: UUID,
        state: dict[str, Any],
    ) -> TeamWorkerSessionRecord: ...

    async def complete_board_task(
        self,
        *,
        session_id: UUID,
        task_id: UUID,
        state: dict[str, Any],
        worker_report: str,
    ) -> BoardTaskRecord: ...

    async def fail_board_task(
        self,
        *,
        session_id: UUID,
        task_id: UUID,
        state: dict[str, Any],
        error: str,
    ) -> BoardTaskRecord: ...

    async def review_board_task(
        self,
        *,
        lead_conversation_id: UUID,
        task_id: UUID,
        accepted: bool,
        feedback: str,
    ) -> BoardTaskRecord: ...

    async def resolve_board_task(
        self,
        *,
        lead_conversation_id: UUID,
        task_id: UUID,
        resolution: Literal["accept_partial", "cancel"],
        reason: str,
    ) -> BoardTaskRecord: ...

    # 长期记忆
    async def remember_cowork_memory(
        self,
        *,
        scope: MemoryScope,
        conversation_id: UUID | None,
        workspace_path: str | None,
        key: str | None,
        content: str,
        source: Literal["agent", "user"],
        category: MemoryCategory = "fact",
        confidence: float = 1.0,
        pinned: bool = False,
        valid_from: datetime | None = None,
        source_message_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> tuple[CoworkMemoryRecord, CoworkMemoryRecord | None]: ...

    async def update_cowork_memory(
        self, *, memory_id: UUID, content: str | None, restore: bool
    ) -> tuple[CoworkMemoryRecord, CoworkMemoryRecord]: ...

    async def forget_cowork_memory(self, *, memory_id: UUID) -> CoworkMemoryRecord | None: ...

    async def get_cowork_memory(self, *, memory_id: UUID) -> CoworkMemoryRecord | None: ...

    async def list_cowork_memories(
        self,
        *,
        conversation_id: UUID,
        workspace_paths: list[str],
        include_forgotten: bool,
        limit: int,
    ) -> list[CoworkMemoryRecord]: ...

    async def supersede_cowork_memory(
        self, *, memory_id: UUID, successor_id: UUID | None, invalid_at: datetime
    ) -> CoworkMemoryRecord | None: ...

    async def set_cowork_memory_pinned(
        self, *, memory_id: UUID, pinned: bool
    ) -> CoworkMemoryRecord | None: ...

    async def touch_cowork_memories(self, *, memory_ids: list[UUID]) -> None: ...

    async def list_cowork_memories_by_validity(
        self, *, active: bool, limit: int
    ) -> list[CoworkMemoryRecord]: ...

    # 记忆抽取作业
    async def schedule_memory_extraction(
        self,
        *,
        run_id: UUID,
        conversation_id: UUID | None,
        source_message_id: UUID | None,
        content: str,
        source_created_at: datetime,
    ) -> MemoryExtractionJob | None: ...

    async def claim_memory_job(
        self, *, job_id: UUID, worker_id: str, lease_s: int, max_attempts: int
    ) -> MemoryExtractionJob | None: ...

    async def complete_memory_job(self, *, job_id: UUID, worker_id: str) -> bool: ...

    async def retry_or_fail_memory_job(
        self,
        *,
        job_id: UUID,
        worker_id: str,
        error: str,
        max_attempts: int,
        retry_delay_s: int,
    ) -> str | None: ...

    async def list_dispatchable_memory_jobs(
        self, *, max_attempts: int, limit: int = 100
    ) -> list[tuple[UUID, int]]: ...

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

    async def list_plan_steps(self, *, run_id: UUID) -> list[dict[str, Any]]: ...

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

    async def set_run_sleeping(
        self, *, run_id: UUID, worker_id: str, wake_at: datetime
    ) -> bool: ...

    async def schedule_run_retry(
        self,
        *,
        run_id: UUID,
        worker_id: str,
        max_recovery: int,
        base_delay_s: float,
        max_delay_s: float,
    ) -> tuple[int, datetime] | None: ...

    async def claim_due_sleeping_runs(self, *, now: datetime, limit: int) -> list[UUID]: ...

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

    async def finish_run_with_events(
        self,
        *,
        run_id: UUID,
        status: str,
        events: Sequence[tuple[str, dict[str, Any]]],
        worker_id: str | None = None,
        error: str | None = None,
        used_tokens: int = 0,
        used_calls: int = 0,
    ) -> tuple[bool, list[RunEvent]]: ...

    async def request_cancel(self, *, run_id: UUID) -> RunRecord: ...

    async def reap_expired_runs(self, *, limit: int, max_recovery: int) -> dict[str, Any]: ...
