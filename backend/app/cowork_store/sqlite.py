"""SQLite Cowork 控制面实现。

每个操作使用独立连接和短事务；模型、浏览器、Shell 等慢操作绝不持有 SQLite 锁。
WAL 允许 SSE/客户端读取与 worker 写入并行，busy_timeout 处理极短的写竞争。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar, cast
from uuid import UUID

from uuid6 import uuid7

from app.agent_core.contracts import (
    TERMINAL_RUN_STATUSES,
    InvocationLease,
    RunEvent,
    RunRecord,
    WorkflowType,
)
from app.agent_core.errors import RunNotFoundError
from app.agent_core.idempotency import (
    InvocationInFlightError,
    InvocationOutcomeUnknownError,
    canonical_json,
    invocation_identity,
)
from app.agent_core.session_entries import SessionEntry, SessionEntryKind
from app.agent_core.session_records import (
    SessionRecord,
    SessionRecordKind,
    SessionRecordPhase,
)
from app.cowork_contracts import (
    DEFAULT_TEAM_BUDGET_LIMITS,
    MAX_STANDING_RULES_CHARS,
    MEMORY_JOB_ERROR_MAX_CHARS,
    AnnotationColor,
    ApprovalMatchKind,
    ApprovalMode,
    ApprovalRuleRecord,
    ApprovalRuleScope,
    ArtifactRecord,
    ArtifactRegistrationError,
    BoardTaskRecord,
    CapabilityDeniedError,
    CapabilityGrantRecord,
    ChannelSubscriptionRecord,
    ConversationBusyError,
    ConversationMemoryPolicy,
    ConversationNotFoundError,
    CoworkAttachmentError,
    CoworkAttachmentRecord,
    CoworkMemoryMutation,
    CoworkMemoryRecord,
    InboxBindingRecord,
    InboxRecord,
    MemoryExtractionJob,
    MemoryNotFoundError,
    MemoryPolicyConflictError,
    MemoryPolicyMode,
    MemoryPolicySnapshot,
    OwnerMemoryPolicy,
    PathAuthorization,
    PinnedMemoryError,
    QueuedMessageDelivery,
    QueuedMessageStatus,
    ReadingAnnotationRecord,
    ScheduleRecord,
    ScheduleView,
    SessionRootNotFoundError,
    SessionRootRecord,
    SteeringRecord,
    SteeringSource,
    TeamBudgetDimension,
    TeamBudgetExceededError,
    TeamBudgetReservationRecord,
    TeamEventCursorRecord,
    TeamEventIntegrityError,
    TeamEventRecord,
    TeamEventVerification,
    TeamProjectionSummaryRecord,
    TeamRecord,
    TeamWakeDeliveryRecord,
    TeamWorkerRecord,
    TeamWorkerSessionRecord,
    TeamWorkerToolAttemptRecord,
    ThreadSessionRecord,
    UnattendedInboxRecord,
    UnroutedRecord,
    normalize_memory_job_result,
)
from app.cowork_policy import (
    ALL_CAPABILITIES,
    GLOBAL_CAPABILITIES,
    LEGACY_CAPABILITY_FALLBACKS,
    PATH_CAPABILITIES,
    SCOPED_CAPABILITIES,
    canonicalize_root,
    network_scope_allows,
    normalize_network_scope,
    resolve_target_within_root,
)
from app.cowork_store.base import SessionLaneNavigation, StoredCheckpoint
from app.cowork_store.run_config import merge_cowork_state, split_cowork_state
from app.run_events import RunEventDraft, RunEventType, run_event
from app.security.redaction import redact_persisted_tool_value

T = TypeVar("T")

_TEAM_EVENT_GENESIS = "0" * 64
_TEAM_EVENT_SCHEMA_VERSION = 1
_TEAM_WAKE_WORKER_EVENTS = frozenset({"board.task.assigned", "board.task.rework_requested"})
_TEAM_WAKE_LEAD_EVENTS = frozenset(
    {"board.task.submitted", "board.task.blocked", "board.task.failed"}
)
_TEAM_WAKE_CONSUMER = "team-wake-dispatcher"
_TEAM_TOOL_ATTEMPT_RESULT_MAX_CHARS = 22_000
_TEAM_TOOL_ATTEMPT_RECEIPT_MAX_CHARS = 64_000
_TEAM_WORKER_LAST_ERROR_MAX_CHARS = 500
_CURRENT_SCHEMA_VERSION = 26
_MEMORY_JOB_BARE_CREDENTIALS = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?<![A-Fa-f0-9])[A-Fa-f0-9]{32,}(?![A-Fa-f0-9])",
        r"(?<![A-Za-z0-9+/_=-])(?=[A-Za-z0-9+/_=-]{32,}(?![A-Za-z0-9+/_=-]))"
        r"(?=[A-Za-z0-9+/_=-]*[A-Z])(?=[A-Za-z0-9+/_=-]*[a-z])"
        r"(?=[A-Za-z0-9+/_=-]*[0-9])[A-Za-z0-9+/_-]{32,}={0,2}",
    )
)

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    provider_profile_id TEXT,
    model_override TEXT,
    unattended INTEGER NOT NULL DEFAULT 0 CHECK (unattended IN (0, 1)),
    approval_mode TEXT NOT NULL DEFAULT 'interactive'
        CHECK (approval_mode IN ('interactive', 'auto')),
    persona_name TEXT NOT NULL DEFAULT 'general',
    -- 路由到哪个命名 Inbox；空表示 "default"。
    inbox_name TEXT,
    -- 这个会话挂载了哪个本地知识库（KB slug）；空表示没挂。
    kb_slug TEXT,
    archived_at TEXT,
    summary TEXT,
    summary_upto INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cowork_conversation_skill_mutes (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    skill_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (conversation_id, skill_name)
);

CREATE TABLE IF NOT EXISTS conversation_message_index (
    record_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    run_id TEXT,
    jsonl_offset INTEGER,
    jsonl_length INTEGER,
    -- 会话列表只需要一小段摘要。正文仍以 JSONL 为准；这里避免每次打开列表都重扫
    -- 最多 100 份完整对话文件。
    content_preview TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (conversation_id, seq)
);

CREATE TABLE IF NOT EXISTS session_entries (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES session_entries(id) ON DELETE RESTRICT,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN (
        'message','model_change','thinking_level_change','active_tools_change',
        'compaction','branch_summary','custom'
    )),
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (conversation_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_session_entries_conversation
ON session_entries(conversation_id, seq);

CREATE TABLE IF NOT EXISTS session_lanes (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    head_entry_id TEXT REFERENCES session_entries(id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (conversation_id, name)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    worker_id TEXT,
    lease_until TEXT,
    heartbeat_at TEXT,
    cancel_requested_at TEXT,
    -- 自唤醒：status='sleeping' 时到点由调度 tick 重新入队。
    wake_at TEXT,
    budget_tokens INTEGER NOT NULL,
    budget_calls INTEGER NOT NULL,
    budget_wall_ms INTEGER NOT NULL,
    used_tokens INTEGER NOT NULL DEFAULT 0,
    used_calls INTEGER NOT NULL DEFAULT 0,
    next_seq INTEGER NOT NULL DEFAULT 1,
    error TEXT,
    answer_mode TEXT NOT NULL DEFAULT 'general',
    workflow_type TEXT NOT NULL DEFAULT 'cowork',
    schedule_id TEXT,
    unattended INTEGER NOT NULL DEFAULT 0 CHECK (unattended IN (0, 1)),
    run_trigger TEXT NOT NULL DEFAULT 'manual',
    source_wake_id TEXT UNIQUE,
    retrieval_top_k INTEGER NOT NULL DEFAULT 5 CHECK (retrieval_top_k BETWEEN 1 AND 20),
    recovery_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_local_runs_dispatch
ON agent_runs(status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_local_runs_lease
ON agent_runs(status, lease_until);
CREATE INDEX IF NOT EXISTS idx_local_runs_conversation_latest
ON agent_runs(conversation_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_local_runs_conversation_active
ON agent_runs(conversation_id, created_at DESC, id DESC)
WHERE status IN ('initializing','queued','executing','waiting_human','sleeping');
CREATE INDEX IF NOT EXISTS idx_local_conversations_recent
ON conversations(archived_at, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_local_messages_run_status
ON conversation_message_index(run_id, status, seq);

CREATE TABLE IF NOT EXISTS session_records (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN ('step_attempt','queue_event','abort_requested','harness_action')
    ),
    operation_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (
        phase IN ('started','completed','failed','enqueued','consumed','cancelled','requested')
    ),
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, seq),
    UNIQUE (run_id, operation_id, phase)
);
CREATE INDEX IF NOT EXISTS idx_session_records_run
ON session_records(run_id, seq);

CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS agent_plan_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_idx INTEGER NOT NULL,
    description TEXT NOT NULL,
    tool TEXT,
    depends_on TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, step_idx)
);

CREATE TABLE IF NOT EXISTS agent_attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    plan_step_id TEXT REFERENCES agent_plan_steps(id) ON DELETE SET NULL,
    attempt_no INTEGER NOT NULL,
    node TEXT NOT NULL,
    tool_name TEXT,
    tool_args TEXT,
    tool_result TEXT,
    status TEXT NOT NULL,
    idempotency_key TEXT,
    latency_ms INTEGER,
    tokens INTEGER,
    error_model TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, plan_step_id, attempt_no, node)
);

CREATE TABLE IF NOT EXISTS agent_checkpoints (
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    checkpoint_id TEXT NOT NULL,
    parent_id TEXT,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, checkpoint_id)
);
CREATE INDEX IF NOT EXISTS idx_local_checkpoints_latest
ON agent_checkpoints(run_id, created_at DESC, checkpoint_id DESC);

-- RunConfig 与每轮变化的 checkpoint 分开：稳定 prompt 前缀和 run 入口事实只存一份。
CREATE TABLE IF NOT EXISTS cowork_run_configs (
    run_id TEXT PRIMARY KEY REFERENCES agent_runs(id) ON DELETE CASCADE,
    config TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_invocations (
    idempotency_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    plan_step_id TEXT,
    tool_name TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    result TEXT,
    status TEXT NOT NULL,
    lease_owner TEXT,
    lease_until TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    effect_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_local_invocations_lease
ON tool_invocations(status, lease_until);

CREATE TABLE IF NOT EXISTS session_roots (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    requested_path TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    label TEXT NOT NULL,
    access_mode TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (conversation_id, canonical_path)
);

CREATE TABLE IF NOT EXISTS capability_grants (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    session_root_id TEXT REFERENCES session_roots(id) ON DELETE CASCADE,
    capability TEXT NOT NULL,
    resource_scope TEXT,
    grant_source TEXT NOT NULL DEFAULT 'user',
    expires_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES agent_runs(id) ON DELETE SET NULL,
    session_root_id TEXT REFERENCES session_roots(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    uri TEXT NOT NULL,
    mime_type TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cowork_attachments (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id TEXT,
    run_id TEXT REFERENCES agent_runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    extracted_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cowork_schedules (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    schedule_kind TEXT NOT NULL,
    cron_expression TEXT,
    run_at TEXT,
    timezone TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run_at TEXT,
    last_run_at TEXT,
    last_run_id TEXT REFERENCES agent_runs(id) ON DELETE SET NULL,
    run_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    dispatch_lease_owner TEXT,
    dispatch_lease_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_local_schedules_due
ON cowork_schedules(enabled, next_run_at, id);

CREATE TABLE IF NOT EXISTS cowork_inbox_bindings (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    -- 要么两个字段都有（有投递绑定），要么都没有（只有应用内 Inbox）。
    platform TEXT,
    chat_id TEXT,
    connector_account_id TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    CHECK ((platform IS NULL) = (chat_id IS NULL))
);

CREATE TABLE IF NOT EXISTS cowork_messaging_event_receipts (
    -- event_key 是 platform + NUL + 上游 event_id 的 SHA-256；不保存原始 id 或 payload。
    event_key TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('claimed', 'completed')),
    received_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_local_messaging_event_receipts_retention
ON cowork_messaging_event_receipts(received_at);

CREATE TABLE IF NOT EXISTS cowork_channel_subscriptions (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    connector_account_id TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_local_channel_subscriptions
ON cowork_channel_subscriptions(platform, chat_id)
WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS cowork_thread_sessions (
    -- 主键就是 thread 地址串：发消息、查会话、判定授权用的是同一个字符串。
    target TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cowork_unrouted (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('inbound', 'background_turn')),
    platform TEXT,
    chat_id TEXT,
    summary TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_local_unrouted_recent ON cowork_unrouted(created_at);

CREATE TABLE IF NOT EXISTS cowork_workspace_trust (
    -- 主键是规范化路径而不是会话：用户信任的是"这个目录"，不是"这一次对话里的这个目录"。
    canonical_path TEXT PRIMARY KEY,
    -- 信任同时绑定用户当时看见的 policy 内容；NULL 只可能来自旧库并按未信任处理。
    policy_sha256 TEXT,
    trusted_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS cowork_approval_rules (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    scope TEXT NOT NULL DEFAULT 'conversation',
    -- 计划派生的规则跟着计划一起消失：删掉计划却留下它攒下的免审批授权，
    -- 是那种"以为已经收回了"的权限。
    schedule_id TEXT REFERENCES cowork_schedules(id) ON DELETE CASCADE,
    tool TEXT NOT NULL,
    match_kind TEXT NOT NULL,
    target TEXT,
    created_by TEXT NOT NULL DEFAULT 'user',
    revoked_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_local_approval_rules
ON cowork_approval_rules(conversation_id, tool)
WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS cowork_inbox_items (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    resume_token TEXT NOT NULL UNIQUE,
    tool_call_id TEXT NOT NULL,
    plan_step_id TEXT NOT NULL REFERENCES agent_plan_steps(id) ON DELETE CASCADE,
    request TEXT NOT NULL,
    response TEXT,
    unattended INTEGER NOT NULL DEFAULT 0,
    -- 这条 item 被镜像到哪里去了。相关靠编在按钮 value 里的 item id，这一列只用于
    -- 「别重复投递」和界面上显示「已发到某个群」。
    delivery_ref TEXT,
    created_at TEXT NOT NULL,
    responded_at TEXT,
    UNIQUE (run_id, tool_call_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_local_pending_inbox
ON cowork_inbox_items(run_id) WHERE status = 'pending';

-- Agent Team 的编制、Worker Session 与 Board 共用同一控制面数据库。Worker Session
-- 预创建时只落一份空闲 JSON state，不触发模型；第一条 Board assignment 才产生调用。
CREATE TABLE IF NOT EXISTS cowork_teams (
    id TEXT PRIMARY KEY,
    lead_conversation_id TEXT NOT NULL UNIQUE
        REFERENCES conversations(id) ON DELETE CASCADE,
    proposal_call_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','paused','archived')),
    note TEXT NOT NULL DEFAULT '',
    write_delegation_scope TEXT NOT NULL DEFAULT '[]',
    write_delegation_receipt TEXT,
    pause_reason TEXT,
    budget_max_model_calls INTEGER NOT NULL DEFAULT 96 CHECK (budget_max_model_calls >= 5),
    budget_max_tool_calls INTEGER NOT NULL DEFAULT 256 CHECK (budget_max_tool_calls >= 0),
    budget_max_wall_ms INTEGER NOT NULL DEFAULT 3600000 CHECK (budget_max_wall_ms >= 30000),
    budget_max_assignments INTEGER NOT NULL DEFAULT 24 CHECK (budget_max_assignments >= 1),
    budget_used_model_calls INTEGER NOT NULL DEFAULT 0 CHECK (budget_used_model_calls >= 0),
    budget_used_tool_calls INTEGER NOT NULL DEFAULT 0 CHECK (budget_used_tool_calls >= 0),
    budget_used_wall_ms INTEGER NOT NULL DEFAULT 0 CHECK (budget_used_wall_ms >= 0),
    budget_used_assignments INTEGER NOT NULL DEFAULT 0 CHECK (budget_used_assignments >= 0),
    budget_reserved_model_calls INTEGER NOT NULL DEFAULT 0 CHECK (budget_reserved_model_calls >= 0),
    budget_reserved_tool_calls INTEGER NOT NULL DEFAULT 0 CHECK (budget_reserved_tool_calls >= 0),
    budget_reserved_wall_ms INTEGER NOT NULL DEFAULT 0 CHECK (budget_reserved_wall_ms >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cowork_team_workers (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES cowork_teams(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE (team_id, name)
);

CREATE TABLE IF NOT EXISTS cowork_team_worker_sessions (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES cowork_teams(id) ON DELETE CASCADE,
    worker_id TEXT NOT NULL UNIQUE REFERENCES cowork_team_workers(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'idle'
        CHECK (status IN ('idle','running','failed')),
    active_task_id TEXT,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cowork_board_tasks (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES cowork_teams(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    acceptance_criteria TEXT NOT NULL,
    resource_scope TEXT NOT NULL DEFAULT '[]',
    scope_receipt TEXT,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','in_progress','blocked','review','done','cancelled')),
    assignee_worker_id TEXT REFERENCES cowork_team_workers(id) ON DELETE SET NULL,
    assignment_call_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    completion_kind TEXT NOT NULL DEFAULT 'pending',
    worker_report TEXT,
    review_comment TEXT,
    last_rejection_comment TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_local_board_tasks_team_status
ON cowork_board_tasks(team_id, status, created_at, id);

CREATE TABLE IF NOT EXISTS cowork_team_budget_reservations (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES cowork_teams(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES cowork_board_tasks(id) ON DELETE CASCADE,
    assignment_call_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','settled')),
    reserved_model_calls INTEGER NOT NULL CHECK (reserved_model_calls >= 0),
    reserved_tool_calls INTEGER NOT NULL CHECK (reserved_tool_calls >= 0),
    reserved_wall_ms INTEGER NOT NULL CHECK (reserved_wall_ms >= 0),
    used_model_calls INTEGER NOT NULL DEFAULT 0 CHECK (used_model_calls >= 0),
    used_tool_calls INTEGER NOT NULL DEFAULT 0 CHECK (used_tool_calls >= 0),
    used_wall_ms INTEGER NOT NULL DEFAULT 0 CHECK (used_wall_ms >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT,
    UNIQUE (team_id, task_id, assignment_call_id)
);
CREATE INDEX IF NOT EXISTS idx_local_team_budget_active
ON cowork_team_budget_reservations(team_id, status);

CREATE TABLE IF NOT EXISTS cowork_team_worker_tool_attempts (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES cowork_teams(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES cowork_team_worker_sessions(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES cowork_board_tasks(id) ON DELETE CASCADE,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    effect TEXT NOT NULL,
    retry_safe INTEGER NOT NULL CHECK (retry_safe IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('in_flight','succeeded','failed','unknown')),
    arguments_sha256 TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count >= 1),
    result TEXT,
    effect_ref TEXT,
    authorization_receipt TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (session_id, task_id, tool_call_id)
);
CREATE INDEX IF NOT EXISTS idx_local_team_tool_attempts_task
ON cowork_team_worker_tool_attempts(task_id, started_at);

-- Team/Board 的写模型仍由上面的 projection 表承载；这里是同事务追加的不可变事实流。
-- 独立 head 能识别只删除尾事件这种单靠 hash linkage 无法发现的篡改。
CREATE TABLE IF NOT EXISTS cowork_team_events (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    cause TEXT NOT NULL,
    parent_event_id TEXT,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (team_id, sequence),
    UNIQUE (team_id, hash)
);
CREATE INDEX IF NOT EXISTS idx_local_team_events_team_sequence
ON cowork_team_events(team_id, sequence);

CREATE TABLE IF NOT EXISTS cowork_team_event_heads (
    team_id TEXT PRIMARY KEY,
    last_sequence INTEGER NOT NULL CHECK (last_sequence > 0),
    last_event_id TEXT NOT NULL,
    head_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cowork_team_event_cursors (
    team_id TEXT NOT NULL,
    consumer TEXT NOT NULL,
    last_sequence INTEGER NOT NULL CHECK (last_sequence >= 0),
    last_event_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (team_id, consumer)
);

-- 每个 Team event 都有一条 durable feed 项，保证 cursor 可以无跳号推进；只有固定
-- allowlist 的行带 lead/worker target，其余行由 dispatcher 持久 ack 为 suppressed。
CREATE TABLE IF NOT EXISTS cowork_team_wake_outbox (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES cowork_teams(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL UNIQUE REFERENCES cowork_team_events(id),
    event_sequence INTEGER NOT NULL CHECK (event_sequence > 0),
    event_hash TEXT NOT NULL,
    event_type TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('none','lead','worker')),
    target_id TEXT,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','claimed','delivered')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    claim_owner TEXT,
    claim_until TEXT,
    validation_outcome TEXT CHECK (validation_outcome IN ('deliver','suppress')),
    validated_at TEXT,
    delivery_receipt TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE (team_id, event_sequence),
    CHECK ((target_kind = 'none' AND target_id IS NULL)
           OR (target_kind <> 'none' AND target_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_local_team_wake_dispatch
ON cowork_team_wake_outbox(status, claim_until, team_id, event_sequence);

CREATE TABLE IF NOT EXISTS cowork_team_event_projection_summaries (
    team_id TEXT PRIMARY KEY,
    watermark INTEGER NOT NULL CHECK (watermark > 0),
    head_hash TEXT NOT NULL,
    summary TEXT NOT NULL,
    rebuilt_at TEXT NOT NULL
);

-- Event rows are immutable during normal operation, but an explicit conversation deletion must
-- also erase privacy-sensitive audit payloads.  The transaction-local guard is populated only by
-- delete_conversation while holding BEGIN IMMEDIATE, then removed before commit.
CREATE TABLE IF NOT EXISTS cowork_team_event_purge_guards (
    team_id TEXT PRIMARY KEY
);

DROP TRIGGER IF EXISTS trg_local_team_events_no_update;
CREATE TRIGGER trg_local_team_events_no_update
BEFORE UPDATE ON cowork_team_events
BEGIN
    SELECT RAISE(ABORT, 'cowork_team_events is append-only');
END;

DROP TRIGGER IF EXISTS trg_local_team_events_no_delete;
CREATE TRIGGER trg_local_team_events_no_delete
BEFORE DELETE ON cowork_team_events
WHEN NOT EXISTS (
    SELECT 1 FROM cowork_team_event_purge_guards AS guards
    WHERE guards.team_id = OLD.team_id
)
BEGIN
    SELECT RAISE(ABORT, 'cowork_team_events is append-only');
END;

CREATE TABLE IF NOT EXISTS cowork_steering_messages (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'unknown' CHECK (
        source IN ('local_owner','external_inbound','runtime','unknown')
    ),
    source_wake_id TEXT UNIQUE,
    requested_delivery TEXT NOT NULL DEFAULT 'steer' CHECK (
        requested_delivery IN ('steer','follow_up','next_run')
    ),
    delivery TEXT NOT NULL DEFAULT 'steer' CHECK (
        delivery IN ('steer','follow_up','next_run')
    ),
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    cancelled_at TEXT
);

CREATE TABLE IF NOT EXISTS cowork_run_queue_state (
    run_id TEXT PRIMARY KEY REFERENCES agent_runs(id) ON DELETE CASCADE,
    follow_up_open INTEGER NOT NULL DEFAULT 1 CHECK (follow_up_open IN (0,1)),
    sealed_at TEXT
);

-- 持久化批注。锚在 material_id（文件内容哈希）上而不是路径：文件改名后批注还在，
-- 文件内容变了则不再出现——locator 与字符区间都可能已经指向别的文字。旧版本的行
-- 不删，按 path 仍可数出来，界面据此说清楚"有 N 条属于旧版本"，而不是静默消失。
CREATE TABLE IF NOT EXISTS cowork_reading_annotations (
    id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL,
    path TEXT NOT NULL,
    locator INTEGER NOT NULL CHECK (locator >= 1),
    quote TEXT NOT NULL,
    note TEXT NOT NULL,
    color TEXT NOT NULL CHECK (color IN ('yellow','green','blue','pink')),
    locations TEXT NOT NULL,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
    run_id TEXT,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_local_reading_annotations_material
ON cowork_reading_annotations(material_id, locator) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_local_reading_annotations_path
ON cowork_reading_annotations(path) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS cowork_memories (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('global','workspace','conversation')),
    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    workspace_path TEXT,
    key TEXT,
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'agent' CHECK (source IN ('agent','user')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    forgotten_at TEXT,
    -- 下面这些是原 PostgreSQL `memories` 表并进来的时序有效性与人工策展字段。
    category TEXT NOT NULL DEFAULT 'fact'
        CHECK (category IN ('preference','profile','interest','fact')),
    confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0,1)),
    -- valid_from 与 created_at 分开：模型可以在今天写下"从上个月起改用表格"。
    valid_from TEXT NOT NULL,
    invalid_at TEXT,
    superseded_by TEXT REFERENCES cowork_memories(id) ON DELETE SET NULL,
    access_count INTEGER NOT NULL DEFAULT 0 CHECK (access_count >= 0),
    last_used_at TEXT,
    source_message_id TEXT,
    run_id TEXT,
    CHECK (
        (scope = 'global' AND conversation_id IS NULL AND workspace_path IS NULL)
        OR (scope = 'workspace' AND conversation_id IS NULL AND workspace_path IS NOT NULL)
        OR (scope = 'conversation' AND conversation_id IS NOT NULL AND workspace_path IS NULL)
    )
);

-- Memory policy 是本机单 owner 控制面，和 learned memories 独立。singleton key 的
-- CHECK 防止未来误插第二份“全局”策略；常驻规则只存 owner 文本，不给 Agent 写入口。
CREATE TABLE IF NOT EXISTS cowork_memory_owner_policy (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    save_enabled INTEGER NOT NULL DEFAULT 1 CHECK (save_enabled IN (0,1)),
    recall_enabled INTEGER NOT NULL DEFAULT 1 CHECK (recall_enabled IN (0,1)),
    standing_rules TEXT NOT NULL DEFAULT ''
        CHECK (length(standing_rules) <= 20000),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cowork_memory_conversation_policies (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    save_mode TEXT NOT NULL DEFAULT 'inherit'
        CHECK (save_mode IN ('inherit','on','off')),
    recall_mode TEXT NOT NULL DEFAULT 'inherit'
        CHECK (recall_mode IN ('inherit','on','off')),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    updated_at TEXT NOT NULL
);

-- 记忆抽取作业。和记忆写在同一个库里，所以"提炼出一条记忆"与"把作业标记完成"
-- 可以在同一个事务里，不会出现记了却没结算、或结算了没记的半截状态。
-- 来源快照只保留到作业终态；done/failed 会清空 content。会话删除时作业随 FK 或显式
-- purge 删除，避免完整用户消息成为孤儿数据。
CREATE TABLE IF NOT EXISTS memory_extraction_jobs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    source_message_id TEXT,
    content TEXT NOT NULL,
    source_created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','done','failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    worker_id TEXT,
    lease_until TEXT,
    available_at TEXT NOT NULL,
    error TEXT,
    result_json TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_local_memory_jobs_dispatch
ON memory_extraction_jobs(available_at, id) WHERE status IN ('queued','running');

"""

_MEMORY_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS ix_local_cowork_memories_active
ON cowork_memories(scope, updated_at) WHERE forgotten_at IS NULL AND invalid_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_local_cowork_memories_history
ON cowork_memories(invalid_at DESC) WHERE invalid_at IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_local_cowork_memories_key
ON cowork_memories(scope, IFNULL(conversation_id, ''), IFNULL(workspace_path, ''), key)
WHERE key IS NOT NULL AND forgotten_at IS NULL AND invalid_at IS NULL;
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).astimezone(UTC).isoformat(timespec="microseconds")


def _datetime(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value).astimezone(UTC)


def _memory_job_retry_error(value: str) -> str:
    """重试期间保留最小诊断；终态一律改成固定错误码。"""

    redacted = str(redact_persisted_tool_value(value))
    for pattern in _MEMORY_JOB_BARE_CREDENTIALS:
        redacted = pattern.sub("<redacted-secret>", redacted)
    normalized = " ".join(redacted.split())
    return normalized[:MEMORY_JOB_ERROR_MAX_CHARS] or "memory_extraction_retry_failed"


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _team_budget_limits(value: dict[str, int] | None) -> dict[str, int]:
    limits = {**DEFAULT_TEAM_BUDGET_LIMITS, **(value or {})}
    required = {"model_calls", "tool_calls", "wall_ms", "assignments"}
    if set(limits) != required or any(type(limits[key]) is not int for key in required):
        raise ValueError("Team budget limits 字段无效")
    if (
        limits["model_calls"] < 5
        or limits["tool_calls"] < 0
        or limits["wall_ms"] < 30_000
        or limits["assignments"] < 1
    ):
        raise ValueError("Team budget limits 低于安全执行下限")
    return limits


def _team_event_hash(
    *,
    event_id: str,
    team_id: str,
    sequence: int,
    event_type: str,
    actor: str,
    cause: str,
    parent_event_id: str | None,
    payload_json: str,
    prev_hash: str,
    created_at: str,
) -> str:
    envelope = {
        "schema_version": _TEAM_EVENT_SCHEMA_VERSION,
        "id": event_id,
        "team_id": team_id,
        "sequence": sequence,
        "event_type": event_type,
        "actor": actor,
        "cause": cause,
        "parent_event_id": parent_event_id,
        # 保留 canonical JSON 字节串而不是反序列化对象，连同 out-of-band whitespace
        # 改写也能被识别；正常写入路径始终生成 canonical_json。
        "payload_json": payload_json,
        "prev_hash": prev_hash,
        "created_at": created_at,
    }
    return hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()


class SqliteCoworkStore:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = path.expanduser()
        self.busy_timeout_ms = busy_timeout_ms
        self._write_lock = asyncio.Lock()
        # 桌面版是单进程；分片锁把需要跨多个短 SQLite 事务的同-run 协议串行化，
        # 同时避免为每个历史 run 永久保留一个 Lock。
        self._run_locks = tuple(asyncio.Lock() for _ in range(64))

    def run_lock(self, run_id: UUID) -> asyncio.Lock:
        return self._run_locks[run_id.int % len(self._run_locks)]

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            database_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if database_version > _CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"cowork.db schema v{database_version} 高于当前应用支持的 "
                    f"v{_CURRENT_SCHEMA_VERSION}，拒绝降级打开"
                )
            connection.executescript(_SCHEMA)
            # purge guard 只允许存在于 delete_conversation 的单个写事务内；启动时清掉任何
            # 异常终止/手工写入留下的 marker，不能把 append-only 例外变成持久能力。
            connection.execute("DELETE FROM cowork_team_event_purge_guards")
            conversation_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(conversations)").fetchall()
            }
            if "archived_at" not in conversation_columns:
                connection.execute("ALTER TABLE conversations ADD COLUMN archived_at TEXT")
            inbox_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(cowork_inbox_items)").fetchall()
            }
            if "delivery_ref" not in inbox_columns:
                connection.execute("ALTER TABLE cowork_inbox_items ADD COLUMN delivery_ref TEXT")
            message_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(conversation_message_index)"
                ).fetchall()
            }
            if "content_preview" not in message_columns:
                connection.execute(
                    "ALTER TABLE conversation_message_index ADD COLUMN content_preview TEXT"
                )
            if "inbox_name" not in conversation_columns:
                connection.execute("ALTER TABLE conversations ADD COLUMN inbox_name TEXT")
            if "approval_mode" not in conversation_columns:
                # ALTER 加不上 CHECK；旧库靠写入路径的取值校验兜住，新库由 _SCHEMA 约束。
                connection.execute(
                    "ALTER TABLE conversations ADD COLUMN approval_mode TEXT "
                    "NOT NULL DEFAULT 'interactive'"
                )
            if "kb_slug" not in conversation_columns:
                connection.execute("ALTER TABLE conversations ADD COLUMN kb_slug TEXT")
            if "persona_name" not in conversation_columns:
                connection.execute(
                    "ALTER TABLE conversations ADD COLUMN persona_name TEXT "
                    "NOT NULL DEFAULT 'general'"
                )
            run_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(agent_runs)").fetchall()
            }
            if "retrieval_top_k" not in run_columns:
                connection.execute(
                    "ALTER TABLE agent_runs ADD COLUMN retrieval_top_k INTEGER NOT NULL DEFAULT 5"
                )
            if "wake_at" not in run_columns:
                connection.execute("ALTER TABLE agent_runs ADD COLUMN wake_at TEXT")
            if "source_wake_id" not in run_columns:
                connection.execute("ALTER TABLE agent_runs ADD COLUMN source_wake_id TEXT")
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_local_runs_source_wake "
                    "ON agent_runs(source_wake_id) WHERE source_wake_id IS NOT NULL"
                )
            steering_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(cowork_steering_messages)"
                ).fetchall()
            }
            if "source_wake_id" not in steering_columns:
                connection.execute(
                    "ALTER TABLE cowork_steering_messages ADD COLUMN source_wake_id TEXT"
                )
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_local_steering_source_wake "
                    "ON cowork_steering_messages(source_wake_id) "
                    "WHERE source_wake_id IS NOT NULL"
                )
            if "source" not in steering_columns:
                connection.execute(
                    "ALTER TABLE cowork_steering_messages "
                    "ADD COLUMN source TEXT NOT NULL DEFAULT 'unknown'"
                )
                connection.execute(
                    "UPDATE cowork_steering_messages SET source = 'runtime' "
                    "WHERE source_wake_id IS NOT NULL"
                )
            if "requested_delivery" not in steering_columns:
                connection.execute(
                    "ALTER TABLE cowork_steering_messages "
                    "ADD COLUMN requested_delivery TEXT NOT NULL DEFAULT 'steer'"
                )
            if "delivery" not in steering_columns:
                connection.execute(
                    "ALTER TABLE cowork_steering_messages "
                    "ADD COLUMN delivery TEXT NOT NULL DEFAULT 'steer'"
                )
            if "cancelled_at" not in steering_columns:
                connection.execute(
                    "ALTER TABLE cowork_steering_messages ADD COLUMN cancelled_at TEXT"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_cowork_queued_messages_dispatch "
                "ON cowork_steering_messages(delivery, status, created_at, id)"
            )
            grant_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(capability_grants)").fetchall()
            }
            if "resource_scope" not in grant_columns:
                connection.execute("ALTER TABLE capability_grants ADD COLUMN resource_scope TEXT")
            workspace_trust_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(cowork_workspace_trust)"
                ).fetchall()
            }
            if "policy_sha256" not in workspace_trust_columns:
                # 旧记录没有证据说明用户看过哪一版 policy；保留行但让 NULL fail closed。
                connection.execute(
                    "ALTER TABLE cowork_workspace_trust ADD COLUMN policy_sha256 TEXT"
                )
            # 旧索引会把同一 capability 的不同网络 origin 错当成一条授权。
            connection.execute("DROP INDEX IF EXISTS uq_local_active_grant")
            connection.execute(
                """CREATE UNIQUE INDEX uq_local_active_grant
                   ON capability_grants(
                       conversation_id,
                       IFNULL(session_root_id, ''),
                       capability,
                       IFNULL(resource_scope, '')
                   ) WHERE revoked_at IS NULL"""
            )
            memory_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(cowork_memories)").fetchall()
            }
            # 两套记忆合并带来的新列。ALTER 加不上 CHECK 也加不上 NOT NULL 无默认值，
            # 所以 valid_from 先给空默认、再用 created_at 回填——已有记忆的生效时刻
            # 就是它被记下的时刻，这是唯一说得通的推断。
            for column, ddl in (
                ("category", "TEXT NOT NULL DEFAULT 'fact'"),
                ("confidence", "REAL NOT NULL DEFAULT 1.0"),
                ("pinned", "INTEGER NOT NULL DEFAULT 0"),
                ("valid_from", "TEXT"),
                ("invalid_at", "TEXT"),
                ("superseded_by", "TEXT"),
                ("access_count", "INTEGER NOT NULL DEFAULT 0"),
                ("last_used_at", "TEXT"),
                ("source_message_id", "TEXT"),
                ("run_id", "TEXT"),
            ):
                if column not in memory_columns:
                    connection.execute(f"ALTER TABLE cowork_memories ADD COLUMN {column} {ddl}")
            connection.execute(
                "UPDATE cowork_memories SET valid_from = created_at WHERE valid_from IS NULL"
            )
            # 这些索引引用记忆合并后新增的 invalid_at。必须等旧库补完列再创建，
            # 否则 executescript 会先报 no such column，迁移代码永远没有机会执行。
            connection.executescript(_MEMORY_INDEX_SCHEMA)
            owner_policy_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(cowork_memory_owner_policy)"
                ).fetchall()
            }
            if "revision" not in owner_policy_columns:
                connection.execute(
                    "ALTER TABLE cowork_memory_owner_policy "
                    "ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
                )
            conversation_policy_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(cowork_memory_conversation_policies)"
                ).fetchall()
            }
            if "revision" not in conversation_policy_columns:
                connection.execute(
                    "ALTER TABLE cowork_memory_conversation_policies "
                    "ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
                )
            memory_job_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(memory_extraction_jobs)"
                ).fetchall()
            }
            if "result_json" not in memory_job_columns:
                connection.execute("ALTER TABLE memory_extraction_jobs ADD COLUMN result_json TEXT")
            board_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(cowork_board_tasks)").fetchall()
            }
            for column, ddl in (
                ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
                ("completion_kind", "TEXT NOT NULL DEFAULT 'pending'"),
                ("last_rejection_comment", "TEXT"),
                ("last_error", "TEXT"),
                ("scope_receipt", "TEXT"),
            ):
                if column not in board_columns:
                    connection.execute(f"ALTER TABLE cowork_board_tasks ADD COLUMN {column} {ddl}")
            team_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(cowork_teams)").fetchall()
            }
            for column, ddl in (
                ("write_delegation_scope", "TEXT NOT NULL DEFAULT '[]'"),
                ("write_delegation_receipt", "TEXT"),
                ("pause_reason", "TEXT"),
                ("budget_max_model_calls", "INTEGER NOT NULL DEFAULT 96"),
                ("budget_max_tool_calls", "INTEGER NOT NULL DEFAULT 256"),
                ("budget_max_wall_ms", "INTEGER NOT NULL DEFAULT 3600000"),
                ("budget_max_assignments", "INTEGER NOT NULL DEFAULT 24"),
                ("budget_used_model_calls", "INTEGER NOT NULL DEFAULT 0"),
                ("budget_used_tool_calls", "INTEGER NOT NULL DEFAULT 0"),
                ("budget_used_wall_ms", "INTEGER NOT NULL DEFAULT 0"),
                ("budget_used_assignments", "INTEGER NOT NULL DEFAULT 0"),
                ("budget_reserved_model_calls", "INTEGER NOT NULL DEFAULT 0"),
                ("budget_reserved_tool_calls", "INTEGER NOT NULL DEFAULT 0"),
                ("budget_reserved_wall_ms", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in team_columns:
                    connection.execute(f"ALTER TABLE cowork_teams ADD COLUMN {column} {ddl}")
            # v13 及更早没有尝试次数和独立拒绝字段；已有 assignment 至少算一次，open
            # 任务当前的 review_comment 就是最近拒绝原因。只能保守回填可证明的下界，
            # 不能根据消息文本猜历史到底重试过几轮。
            connection.execute(
                """UPDATE cowork_board_tasks SET attempt_count = 1
                   WHERE attempt_count = 0
                     AND (assignment_call_id IS NOT NULL OR worker_report IS NOT NULL)"""
            )
            connection.execute(
                """UPDATE cowork_board_tasks SET last_rejection_comment = review_comment
                   WHERE last_rejection_comment IS NULL AND status = 'open'
                     AND review_comment IS NOT NULL AND review_comment <> ''"""
            )
            connection.execute(
                """UPDATE cowork_board_tasks SET completion_kind =
                       CASE status WHEN 'done' THEN 'complete'
                                   WHEN 'cancelled' THEN 'cancelled'
                                   ELSE completion_kind END"""
            )
            # v12: run 创建先落 initializing，checkpoint 与初始事件同事务完成后才可派发。
            # 旧版进程如果在初始化途中退出，新的 sidecar 不可能继续那条 HTTP 调用；启动时
            # 明确标失败，避免会话永远被一条不可执行的活跃 run 卡住。
            timestamp = _iso()
            connection.execute(
                """UPDATE agent_runs SET status = 'failed',
                          error = COALESCE(error, 'run initialization interrupted'),
                          finished_at = COALESCE(finished_at, ?), updated_at = ?
                   WHERE status = 'initializing'""",
                (timestamp, timestamp),
            )
            connection.execute("DROP INDEX IF EXISTS idx_local_runs_conversation_active")
            connection.execute(
                """CREATE INDEX idx_local_runs_conversation_active
                   ON agent_runs(conversation_id, created_at DESC, id DESC)
                   WHERE status IN (
                       'initializing','queued','executing','waiting_human','sleeping'
                   )"""
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._migrate_v26_session_records(connection)
                self._backfill_team_event_snapshots(connection)
                self._backfill_team_wake_feed(
                    connection,
                    # executescript(_SCHEMA) 在迁移事务前建表。若第一次 v18 数据迁移
                    # 失败，表会留下但 user_version 仍是 17；重试仍必须把历史 feed
                    # 当作升级前状态 suppress，不能仅凭表已存在将其重新激活。
                    suppress_existing=database_version < 18,
                )
                if database_version < 18:
                    self._migrate_v18_team_state(connection)
                if database_version < 20:
                    self._migrate_v20_memory_state(connection)
                connection.execute(f"PRAGMA user_version = {_CURRENT_SCHEMA_VERSION}")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.close()
        try:
            self.path.chmod(0o600)
            self.path.parent.chmod(0o700)
        except PermissionError:  # pragma: no cover - 不支持 chmod 的文件系统
            pass

    @staticmethod
    def _migrate_v26_session_records(connection: sqlite3.Connection) -> None:
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'session_records'"
        ).fetchone()
        raw_schema = "" if schema is None else str(schema[0] or "")
        if schema is None or all(
            marker in raw_schema for marker in ("queue_event", "harness_action")
        ):
            return
        connection.execute("ALTER TABLE session_records RENAME TO session_records_legacy")
        connection.execute(
            """CREATE TABLE session_records (
                   id TEXT PRIMARY KEY,
                   run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
                   seq INTEGER NOT NULL,
                   kind TEXT NOT NULL CHECK (
                       kind IN (
                           'step_attempt','queue_event','abort_requested','harness_action'
                       )
                   ),
                   operation_id TEXT NOT NULL,
                   phase TEXT NOT NULL CHECK (
                       phase IN (
                           'started','completed','failed','enqueued','consumed',
                           'cancelled','requested'
                       )
                   ),
                   payload TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   UNIQUE (run_id, seq),
                   UNIQUE (run_id, operation_id, phase)
               )"""
        )
        connection.execute(
            """INSERT INTO session_records(
                   id, run_id, seq, kind, operation_id, phase, payload, created_at
               )
               SELECT id, run_id, seq, kind, operation_id, phase, payload, created_at
               FROM session_records_legacy"""
        )
        connection.execute("DROP TABLE session_records_legacy")
        connection.execute(
            "CREATE INDEX idx_session_records_run ON session_records(run_id, seq)"
        )

    async def close(self) -> None:
        return None

    @staticmethod
    def _receipt_event_summary(raw_receipt: Any) -> dict[str, Any] | None:
        if raw_receipt is None:
            return None
        receipt = json.loads(str(raw_receipt)) if isinstance(raw_receipt, str) else raw_receipt
        if not isinstance(receipt, dict):
            raise ValueError("Team receipt 必须是 JSON object")
        return {
            "receipt_id": receipt.get("receipt_id"),
            "mechanism": receipt.get("mechanism"),
            "scope_sha256": receipt.get("scope_sha256"),
            "delegation_receipt_id": receipt.get("delegation_receipt_id"),
            "receipt_sha256": _json_sha256(receipt),
        }

    @staticmethod
    def _team_budget_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "limits": {
                "model_calls": int(row["budget_max_model_calls"]),
                "tool_calls": int(row["budget_max_tool_calls"]),
                "wall_ms": int(row["budget_max_wall_ms"]),
                "assignments": int(row["budget_max_assignments"]),
            },
            "used": {
                "model_calls": int(row["budget_used_model_calls"]),
                "tool_calls": int(row["budget_used_tool_calls"]),
                "wall_ms": int(row["budget_used_wall_ms"]),
                "assignments": int(row["budget_used_assignments"]),
            },
            "reserved": {
                "model_calls": int(row["budget_reserved_model_calls"]),
                "tool_calls": int(row["budget_reserved_tool_calls"]),
                "wall_ms": int(row["budget_reserved_wall_ms"]),
            },
        }

    def _team_event_summary(self, row: sqlite3.Row) -> dict[str, Any]:
        raw_scope = json.loads(str(row["write_delegation_scope"] or "[]"))
        if not isinstance(raw_scope, list):
            raise ValueError("Team write_delegation_scope 必须是 JSON array")
        return {
            "id": str(row["id"]),
            "lead_conversation_id": str(row["lead_conversation_id"]),
            "proposal_call_id": str(row["proposal_call_id"]),
            "status": str(row["status"]),
            "pause_reason": row["pause_reason"],
            "note": str(row["note"]),
            "write_delegation_scope": raw_scope,
            "write_delegation_receipt": self._receipt_event_summary(
                row["write_delegation_receipt"]
            ),
            "budget": self._team_budget_summary(row),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def _team_event_projection_snapshot(
        self, connection: sqlite3.Connection, team_row: sqlite3.Row
    ) -> dict[str, Any]:
        team_id = str(team_row["id"])
        raw_scope = json.loads(str(team_row["write_delegation_scope"] or "[]"))
        if not isinstance(raw_scope, list):
            raise ValueError("Team write_delegation_scope 必须是 JSON array")
        team_receipt = self._receipt_event_summary(team_row["write_delegation_receipt"])
        worker_rows = connection.execute(
            """SELECT workers.*, sessions.status AS session_status,
                      sessions.active_task_id, sessions.state AS session_state,
                      sessions.updated_at AS session_updated_at
               FROM cowork_team_workers AS workers
               JOIN cowork_team_worker_sessions AS sessions
                 ON sessions.id = workers.session_id
               WHERE workers.team_id = ? ORDER BY workers.created_at, workers.id""",
            (team_id,),
        ).fetchall()
        task_rows = connection.execute(
            "SELECT * FROM cowork_board_tasks WHERE team_id = ? ORDER BY created_at, id",
            (team_id,),
        ).fetchall()
        workers = [
            {
                "id": str(row["id"]),
                "name": str(row["name"]),
                "role": str(row["role"]),
                "reason": str(row["reason"]),
                "session_id": str(row["session_id"]),
                "session_status": str(row["session_status"]),
                "active_task_id": row["active_task_id"],
                "session_state_sha256": _json_sha256(json.loads(str(row["session_state"]))),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["session_updated_at"]),
            }
            for row in worker_rows
        ]
        tasks = [self._board_task_event_summary(row) for row in task_rows]
        receipts = [] if team_receipt is None else [{"kind": "team_write", **team_receipt}]
        receipts.extend(
            {"kind": "task_scope", "task_id": task["id"], **receipt}
            for task in tasks
            if (receipt := task.get("scope_receipt")) is not None
        )
        return {
            "schema_version": "team-projection-summary.v1",
            "team": self._team_event_summary(team_row),
            "workers": workers,
            "tasks": tasks,
            "receipts": receipts,
            "worker_checkpoint_count": 0,
            "worker_tool_attempt_count": 0,
            "worker_tool_unknown_count": 0,
        }

    def _board_task_event_summary(self, row: sqlite3.Row) -> dict[str, Any]:
        raw_scope = json.loads(str(row["resource_scope"] or "[]"))
        if not isinstance(raw_scope, list):
            raise ValueError("Board task resource_scope 必须是 JSON array")
        return {
            "id": str(row["id"]),
            "team_id": str(row["team_id"]),
            "title": str(row["title"]),
            "description": str(row["description"]),
            "acceptance_criteria": str(row["acceptance_criteria"]),
            "resource_scope": raw_scope,
            "resource_scope_sha256": _json_sha256(raw_scope),
            "scope_receipt": self._receipt_event_summary(row["scope_receipt"]),
            "status": str(row["status"]),
            "assignee_worker_id": row["assignee_worker_id"],
            "assignment_call_id": row["assignment_call_id"],
            "attempt_count": int(row["attempt_count"]),
            "completion_kind": str(row["completion_kind"]),
            "worker_report": row["worker_report"],
            "review_comment": row["review_comment"],
            "last_rejection_comment": row["last_rejection_comment"],
            "last_error": row["last_error"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _team_wake_target_transaction(
        connection: sqlite3.Connection,
        *,
        team_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> tuple[Literal["none", "lead", "worker"], str | None]:
        if event_type in _TEAM_WAKE_WORKER_EVENTS:
            worker_id = payload.get("worker_id")
            if not isinstance(worker_id, str) or not worker_id:
                raise TeamEventIntegrityError(f"{event_type} 缺少 Worker wake target")
            return "worker", worker_id
        if event_type in _TEAM_WAKE_LEAD_EVENTS:
            team = connection.execute(
                "SELECT lead_conversation_id FROM cowork_teams WHERE id = ?", (team_id,)
            ).fetchone()
            if team is None:
                raise TeamEventIntegrityError(f"{event_type} 缺少 Lead wake target")
            return "lead", str(team["lead_conversation_id"])
        return "none", None

    def _append_team_events_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        team_id: UUID | str,
        actor: str,
        cause: str,
        events: Sequence[tuple[str, dict[str, Any]]],
        created_at: str | None = None,
    ) -> list[TeamEventRecord]:
        if not events:
            return []
        normalized_actor = actor.strip()
        normalized_cause = cause.strip()
        if not normalized_actor or not normalized_cause:
            raise ValueError("Team event actor/cause 不能为空")
        team_key = str(team_id)
        head = connection.execute(
            "SELECT * FROM cowork_team_event_heads WHERE team_id = ?", (team_key,)
        ).fetchone()
        if head is None:
            stray = connection.execute(
                "SELECT id FROM cowork_team_events WHERE team_id = ? LIMIT 1", (team_key,)
            ).fetchone()
            if stray is not None:
                raise TeamEventIntegrityError("Team event head 缺失但日志非空")
            sequence = 0
            previous_id: str | None = None
            previous_hash = _TEAM_EVENT_GENESIS
        else:
            tail = connection.execute(
                """SELECT id, sequence, hash FROM cowork_team_events
                   WHERE team_id = ? ORDER BY sequence DESC LIMIT 1""",
                (team_key,),
            ).fetchone()
            if (
                tail is None
                or int(tail["sequence"]) != int(head["last_sequence"])
                or str(tail["id"]) != str(head["last_event_id"])
                or str(tail["hash"]) != str(head["head_hash"])
            ):
                raise TeamEventIntegrityError("Team event tail 与独立 head 不一致")
            sequence = int(head["last_sequence"])
            previous_id = str(head["last_event_id"])
            previous_hash = str(head["head_hash"])

        timestamp = created_at or _iso()
        stored: list[TeamEventRecord] = []
        for event_type, payload in events:
            normalized_type = event_type.strip()
            if not normalized_type or not isinstance(payload, dict):
                raise ValueError("Team event type 必须非空且 payload 必须是 object")
            sequence += 1
            event_id = str(uuid7())
            payload_json = canonical_json(payload)
            event_hash = _team_event_hash(
                event_id=event_id,
                team_id=team_key,
                sequence=sequence,
                event_type=normalized_type,
                actor=normalized_actor,
                cause=normalized_cause,
                parent_event_id=previous_id,
                payload_json=payload_json,
                prev_hash=previous_hash,
                created_at=timestamp,
            )
            connection.execute(
                """INSERT INTO cowork_team_events(
                       id, team_id, sequence, event_type, actor, cause, parent_event_id,
                       payload, prev_hash, hash, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    team_key,
                    sequence,
                    normalized_type,
                    normalized_actor,
                    normalized_cause,
                    previous_id,
                    payload_json,
                    previous_hash,
                    event_hash,
                    timestamp,
                ),
            )
            target_kind, target_id = self._team_wake_target_transaction(
                connection,
                team_id=team_key,
                event_type=normalized_type,
                payload=payload,
            )
            connection.execute(
                """INSERT INTO cowork_team_wake_outbox(
                       id, team_id, event_id, event_sequence, event_hash, event_type,
                       target_kind, target_id, payload, status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    str(uuid7()),
                    team_key,
                    event_id,
                    sequence,
                    event_hash,
                    normalized_type,
                    target_kind,
                    target_id,
                    payload_json,
                    timestamp,
                    timestamp,
                ),
            )
            stored.append(
                TeamEventRecord(
                    id=UUID(event_id),
                    team_id=UUID(team_key),
                    sequence=sequence,
                    event_type=normalized_type,
                    actor=normalized_actor,
                    cause=normalized_cause,
                    parent_event_id=(None if previous_id is None else UUID(previous_id)),
                    payload=json.loads(payload_json),
                    prev_hash=previous_hash,
                    hash=event_hash,
                    created_at=datetime.fromisoformat(timestamp).astimezone(UTC),
                )
            )
            previous_id = event_id
            previous_hash = event_hash

        connection.execute(
            """INSERT INTO cowork_team_event_heads(
                   team_id, last_sequence, last_event_id, head_hash, updated_at
               ) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(team_id) DO UPDATE SET
                   last_sequence = excluded.last_sequence,
                   last_event_id = excluded.last_event_id,
                   head_hash = excluded.head_hash,
                   updated_at = excluded.updated_at""",
            (team_key, sequence, previous_id, previous_hash, timestamp),
        )
        return stored

    def _backfill_team_wake_feed(
        self, connection: sqlite3.Connection, *, suppress_existing: bool
    ) -> None:
        """升级时补齐 feed；历史事件不会在安装新版本后突然唤醒旧任务。"""

        rows = connection.execute(
            """SELECT events.* FROM cowork_team_events AS events
               LEFT JOIN cowork_team_wake_outbox AS wake ON wake.event_id = events.id
               WHERE wake.id IS NULL ORDER BY events.team_id, events.sequence"""
        ).fetchall()
        timestamp = _iso()
        for row in rows:
            payload = json.loads(str(row["payload"]))
            if not isinstance(payload, dict):
                raise TeamEventIntegrityError("Team wake backfill event payload 非法")
            target_kind, target_id = self._team_wake_target_transaction(
                connection,
                team_id=str(row["team_id"]),
                event_type=str(row["event_type"]),
                payload=payload,
            )
            connection.execute(
                """INSERT INTO cowork_team_wake_outbox(
                       id, team_id, event_id, event_sequence, event_hash, event_type,
                       target_kind, target_id, payload, status, delivery_receipt,
                       created_at, updated_at, delivered_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid7()),
                    str(row["team_id"]),
                    str(row["id"]),
                    int(row["sequence"]),
                    str(row["hash"]),
                    str(row["event_type"]),
                    target_kind,
                    target_id,
                    canonical_json(payload),
                    "delivered" if suppress_existing else "pending",
                    "migration:suppressed-existing" if suppress_existing else None,
                    str(row["created_at"]),
                    timestamp,
                    timestamp if suppress_existing else None,
                ),
            )
        if not suppress_existing:
            return
        # _backfill_team_event_snapshots 可能刚在同一事务内追加了 snapshot feed；它同样
        # 属于升级前状态。cursor 与 suppressed rows 一次提交，下一条 v18 事件才可投递。
        connection.execute(
            """UPDATE cowork_team_wake_outbox SET status = 'delivered',
                      claim_owner = NULL, claim_until = NULL,
                      delivery_receipt = COALESCE(
                          delivery_receipt, 'migration:suppressed-existing'
                      ),
                      delivered_at = COALESCE(delivered_at, ?), updated_at = ?
               WHERE status <> 'delivered'""",
            (timestamp, timestamp),
        )
        heads = connection.execute("SELECT * FROM cowork_team_event_heads").fetchall()
        for head in heads:
            connection.execute(
                """INSERT INTO cowork_team_event_cursors(
                       team_id, consumer, last_sequence, last_event_hash, updated_at
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(team_id, consumer) DO UPDATE SET
                       last_sequence = excluded.last_sequence,
                       last_event_hash = excluded.last_event_hash,
                       updated_at = excluded.updated_at""",
                (
                    str(head["team_id"]),
                    _TEAM_WAKE_CONSUMER,
                    int(head["last_sequence"]),
                    str(head["head_hash"]),
                    timestamp,
                ),
            )

    def _backfill_team_event_snapshots(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """SELECT teams.* FROM cowork_teams AS teams
               LEFT JOIN cowork_team_event_heads AS heads ON heads.team_id = teams.id
               WHERE heads.team_id IS NULL ORDER BY teams.created_at, teams.id"""
        ).fetchall()
        for row in rows:
            snapshot = self._team_event_projection_snapshot(connection, row)
            self._append_team_events_transaction(
                connection,
                team_id=str(row["id"]),
                actor="system:migration",
                cause="schema:v17",
                events=[
                    (
                        "team.projection_imported",
                        {
                            "source_schema_version": 16,
                            "summary": snapshot,
                        },
                    )
                ],
                created_at=_iso(),
            )

    def _migrate_v18_team_state(self, connection: sqlite3.Connection) -> None:
        """为 v17 Team 建立累计预算；旧的 in-flight 写结果未知，保守转 blocked。"""

        timestamp = _iso()
        connection.execute(
            """UPDATE cowork_teams SET budget_used_assignments =
                   COALESCE((SELECT SUM(tasks.attempt_count) FROM cowork_board_tasks AS tasks
                             WHERE tasks.team_id = cowork_teams.id), 0)"""
        )
        connection.execute(
            """UPDATE cowork_teams SET budget_max_assignments = budget_used_assignments
               WHERE budget_used_assignments > budget_max_assignments"""
        )
        teams = connection.execute("SELECT * FROM cowork_teams ORDER BY created_at, id").fetchall()
        for team_row in teams:
            team_id = str(team_row["id"])
            events: list[tuple[str, dict[str, Any]]] = []
            running = connection.execute(
                """SELECT tasks.*, sessions.id AS session_id,
                          sessions.state AS session_state, sessions.worker_id AS worker_id
                   FROM cowork_board_tasks AS tasks
                   JOIN cowork_team_worker_sessions AS sessions
                     ON sessions.active_task_id = tasks.id
                   WHERE tasks.team_id = ? AND tasks.status = 'in_progress'
                     AND sessions.status = 'running'""",
                (team_id,),
            ).fetchall()
            for task in running:
                reason = (
                    "v18 恢复边界：旧版本未记录 Worker 内部工具 attempt，"
                    "崩溃点副作用结果未知；已阻塞，需 Lead 核验后显式重试"
                )
                connection.execute(
                    """UPDATE cowork_board_tasks SET status = 'blocked', last_error = ?,
                              updated_at = ? WHERE id = ?""",
                    (reason, timestamp, str(task["id"])),
                )
                connection.execute(
                    """UPDATE cowork_team_worker_sessions SET status = 'idle',
                              active_task_id = NULL, updated_at = ? WHERE id = ?""",
                    (timestamp, str(task["session_id"])),
                )
                updated = connection.execute(
                    "SELECT * FROM cowork_board_tasks WHERE id = ?", (str(task["id"]),)
                ).fetchone()
                assert updated is not None
                events.append(
                    (
                        "board.task.blocked",
                        {
                            "task": self._board_task_event_summary(updated),
                            "worker_id": str(task["worker_id"]),
                            "session_id": str(task["session_id"]),
                            "session_state_sha256": _json_sha256(
                                json.loads(str(task["session_state"]))
                            ),
                            "migration_fail_closed": True,
                        },
                    )
                )
            refreshed = connection.execute(
                "SELECT * FROM cowork_teams WHERE id = ?", (team_id,)
            ).fetchone()
            assert refreshed is not None
            events.append(
                (
                    "team.budget_initialized",
                    {
                        "team": self._team_event_summary(refreshed),
                        "migration_from_schema": 17,
                    },
                )
            )
            self._append_team_events_transaction(
                connection,
                team_id=team_id,
                actor="system:migration",
                cause="schema:v18",
                events=events,
                created_at=timestamp,
            )

    @staticmethod
    def _migrate_v20_memory_state(connection: sqlite3.Connection) -> None:
        """清掉旧 orphan，并把历史终态作业收敛成无用户原文的最小审计记录。"""

        connection.execute(
            """DELETE FROM memory_extraction_jobs
               WHERE conversation_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM conversations
                     WHERE conversations.id = memory_extraction_jobs.conversation_id
                 )"""
        )
        connection.execute(
            """UPDATE memory_extraction_jobs
               SET content = '',
                   error = CASE status WHEN 'failed' THEN 'memory_extraction_failed' ELSE NULL END
               WHERE status IN ('done', 'failed')"""
        )

    @staticmethod
    def _team_event_record(row: sqlite3.Row) -> TeamEventRecord:
        payload = json.loads(str(row["payload"]))
        if not isinstance(payload, dict):
            raise TeamEventIntegrityError(f"Team event {row['sequence']} payload 不是 JSON object")
        return TeamEventRecord(
            id=UUID(str(row["id"])),
            team_id=UUID(str(row["team_id"])),
            sequence=int(row["sequence"]),
            event_type=str(row["event_type"]),
            actor=str(row["actor"]),
            cause=str(row["cause"]),
            parent_event_id=(
                None if row["parent_event_id"] is None else UUID(str(row["parent_event_id"]))
            ),
            payload=payload,
            prev_hash=str(row["prev_hash"]),
            hash=str(row["hash"]),
            created_at=datetime.fromisoformat(str(row["created_at"])).astimezone(UTC),
        )

    def _verified_team_event_rows(
        self, connection: sqlite3.Connection, team_id: UUID | str
    ) -> tuple[list[sqlite3.Row], sqlite3.Row]:
        team_key = str(team_id)
        rows = connection.execute(
            "SELECT * FROM cowork_team_events WHERE team_id = ? ORDER BY sequence",
            (team_key,),
        ).fetchall()
        head = connection.execute(
            "SELECT * FROM cowork_team_event_heads WHERE team_id = ?", (team_key,)
        ).fetchone()
        if not rows or head is None:
            raise TeamEventIntegrityError("Team event log/head 缺失")
        expected_sequence = 1
        previous_id: str | None = None
        previous_hash = _TEAM_EVENT_GENESIS
        for row in rows:
            sequence = int(row["sequence"])
            if sequence != expected_sequence:
                raise TeamEventIntegrityError(f"Team event sequence 在 {expected_sequence} 处断裂")
            parent = None if row["parent_event_id"] is None else str(row["parent_event_id"])
            if parent != previous_id:
                raise TeamEventIntegrityError(f"Team event {sequence} parent linkage 已损坏")
            if str(row["prev_hash"]) != previous_hash:
                raise TeamEventIntegrityError(f"Team event {sequence} prev_hash 已损坏")
            payload_json = str(row["payload"])
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError as error:
                raise TeamEventIntegrityError(
                    f"Team event {sequence} payload 不是合法 JSON"
                ) from error
            if not isinstance(payload, dict):
                raise TeamEventIntegrityError(f"Team event {sequence} payload 不是 JSON object")
            calculated = _team_event_hash(
                event_id=str(row["id"]),
                team_id=str(row["team_id"]),
                sequence=sequence,
                event_type=str(row["event_type"]),
                actor=str(row["actor"]),
                cause=str(row["cause"]),
                parent_event_id=parent,
                payload_json=payload_json,
                prev_hash=str(row["prev_hash"]),
                created_at=str(row["created_at"]),
            )
            if calculated != str(row["hash"]):
                raise TeamEventIntegrityError(f"Team event {sequence} 内容与 hash 不匹配")
            previous_id = str(row["id"])
            previous_hash = str(row["hash"])
            expected_sequence += 1
        if (
            int(head["last_sequence"]) != len(rows)
            or str(head["last_event_id"]) != previous_id
            or str(head["head_hash"]) != previous_hash
        ):
            raise TeamEventIntegrityError("Team event log 尾部与独立 head 不一致")
        return rows, head

    @staticmethod
    def _fold_team_event_projection(events: Sequence[TeamEventRecord]) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "schema_version": "team-projection-summary.v1",
            "team": None,
            "workers": [],
            "tasks": [],
            "receipts": [],
            "worker_checkpoint_count": 0,
            "worker_tool_attempt_count": 0,
            "worker_tool_unknown_count": 0,
        }
        event_counts: dict[str, int] = {}
        for event in events:
            event_counts[event.event_type] = event_counts.get(event.event_type, 0) + 1
            payload = event.payload
            if event.event_type == "team.projection_imported":
                imported = payload.get("summary")
                if not isinstance(imported, dict):
                    raise TeamEventIntegrityError(
                        f"Team event {event.sequence} migration summary 非法"
                    )
                summary = json.loads(canonical_json(imported))
                continue
            if event.event_type == "team.created":
                team = payload.get("team")
                workers = payload.get("workers")
                if not isinstance(team, dict) or not isinstance(workers, list):
                    raise TeamEventIntegrityError(
                        f"Team event {event.sequence} create payload 非法"
                    )
                summary["team"] = team
                summary["workers"] = workers
                continue
            if event.event_type.startswith("team."):
                team = payload.get("team")
                if isinstance(team, dict):
                    current_team = summary.get("team")
                    if isinstance(current_team, dict):
                        current_team.update(team)
                    else:
                        summary["team"] = team
                budget = payload.get("budget")
                current_team = summary.get("team")
                if isinstance(budget, dict) and isinstance(current_team, dict):
                    current_team["budget"] = budget
            task = payload.get("task")
            if isinstance(task, dict):
                tasks = {
                    str(item["id"]): item
                    for item in cast("list[dict[str, Any]]", summary.get("tasks", []))
                    if isinstance(item, dict) and item.get("id") is not None
                }
                tasks[str(task["id"])] = task
                summary["tasks"] = list(tasks.values())
            worker_id = payload.get("worker_id")
            workers = cast("list[dict[str, Any]]", summary.get("workers", []))
            if event.event_type == "board.task.assigned":
                for worker in workers:
                    if str(worker.get("id")) == str(worker_id):
                        worker.update(
                            {
                                "session_status": payload.get("session_status"),
                                "active_task_id": (
                                    task.get("id") if isinstance(task, dict) else None
                                ),
                                "updated_at": event.created_at.isoformat(),
                            }
                        )
                        break
            elif event.event_type in {"board.task.submitted", "board.task.blocked"}:
                for worker in workers:
                    if str(worker.get("id")) == str(worker_id):
                        worker.update(
                            {
                                "session_status": "idle",
                                "active_task_id": None,
                                "session_state_sha256": payload.get("session_state_sha256"),
                                "updated_at": event.created_at.isoformat(),
                            }
                        )
                        break
            receipt = payload.get("receipt")
            if isinstance(receipt, dict):
                receipt_key = (
                    str(receipt.get("kind")),
                    str(receipt.get("task_id") or ""),
                    str(receipt.get("receipt_id") or ""),
                )
                receipts = [
                    item
                    for item in cast("list[dict[str, Any]]", summary.get("receipts", []))
                    if (
                        str(item.get("kind")),
                        str(item.get("task_id") or ""),
                        str(item.get("receipt_id") or ""),
                    )
                    != receipt_key
                ]
                receipts.append(receipt)
                summary["receipts"] = receipts
            if event.event_type == "team.worker_session.checkpointed":
                summary["worker_checkpoint_count"] = (
                    int(summary.get("worker_checkpoint_count", 0)) + 1
                )
                for worker in workers:
                    if str(worker.get("id")) == str(worker_id):
                        worker.update(
                            {
                                "session_status": payload.get("session_status"),
                                "active_task_id": payload.get("task_id"),
                                "session_state_sha256": payload.get("state_sha256"),
                                "updated_at": event.created_at.isoformat(),
                            }
                        )
                        break
            if event.event_type in {
                "team.worker_tool.started",
                "team.worker_tool.retried",
            }:
                summary["worker_tool_attempt_count"] = (
                    int(summary.get("worker_tool_attempt_count", 0)) + 1
                )
            elif event.event_type == "team.worker_tool.unknown":
                summary["worker_tool_unknown_count"] = (
                    int(summary.get("worker_tool_unknown_count", 0)) + 1
                )
            if event.event_type in {
                "team.write_delegation_revoked",
                "team.archived",
            }:
                summary["receipts"] = [
                    item
                    for item in cast("list[dict[str, Any]]", summary.get("receipts", []))
                    if item.get("kind") != "team_write"
                ]
        summary["tasks"] = sorted(
            cast("list[dict[str, Any]]", summary.get("tasks", [])),
            key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")),
        )
        summary["workers"] = sorted(
            cast("list[dict[str, Any]]", summary.get("workers", [])),
            key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")),
        )
        summary["receipts"] = sorted(
            cast("list[dict[str, Any]]", summary.get("receipts", [])),
            key=lambda item: (
                str(item.get("kind") or ""),
                str(item.get("task_id") or ""),
                str(item.get("receipt_id") or ""),
            ),
        )
        summary["event_count"] = len(events)
        summary["events_by_type"] = dict(sorted(event_counts.items()))
        return summary

    def _replay_team_event_projection_transaction(
        self, connection: sqlite3.Connection, team_id: UUID
    ) -> TeamProjectionSummaryRecord:
        rows, head = self._verified_team_event_rows(connection, team_id)
        events = [self._team_event_record(row) for row in rows]
        rebuilt_at = _now()
        return TeamProjectionSummaryRecord(
            team_id=team_id,
            watermark=int(head["last_sequence"]),
            head_hash=str(head["head_hash"]),
            summary=self._fold_team_event_projection(events),
            rebuilt_at=rebuilt_at,
        )

    async def import_rows(
        self,
        *,
        table: str,
        rows: Sequence[dict[str, Any]],
        primary_key: Sequence[str],
    ) -> int:
        """幂等导入 PostgreSQL 快照；只允许本地控制面表。"""

        allowed = {
            "conversations",
            "conversation_message_index",
            "session_entries",
            "session_lanes",
            "session_records",
            "agent_runs",
            "run_events",
            "agent_plan_steps",
            "agent_attempts",
            "agent_checkpoints",
            "cowork_run_configs",
            "tool_invocations",
            "session_roots",
            "capability_grants",
            "artifacts",
            "cowork_attachments",
            "cowork_schedules",
            "cowork_inbox_items",
            "cowork_steering_messages",
            "cowork_run_queue_state",
            "cowork_teams",
            "cowork_team_workers",
            "cowork_team_worker_sessions",
            "cowork_board_tasks",
            "cowork_memories",
        }
        if table not in allowed:
            raise ValueError(f"不允许导入表: {table}")
        if not rows:
            return 0

        def encode(value: Any) -> Any:
            if isinstance(value, UUID):
                return str(value)
            if isinstance(value, datetime):
                return _iso(value)
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, (dict, list, tuple)):
                return canonical_json(value)
            return value

        def operation(connection: sqlite3.Connection) -> int:
            available = {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            columns = [name for name in rows[0] if name in available]
            if not columns or not set(primary_key).issubset(columns):
                raise ValueError(f"{table} 快照缺少主键或可导入列")
            quoted = ", ".join(columns)
            placeholders = ", ".join("?" for _ in columns)
            updates = [name for name in columns if name not in primary_key]
            conflict = ", ".join(primary_key)
            update_sql = ", ".join(f"{name} = excluded.{name}" for name in updates)
            sql = (
                f"INSERT INTO {table}({quoted}) VALUES ({placeholders}) "
                f"ON CONFLICT({conflict}) DO UPDATE SET {update_sql}"
                if updates
                else f"INSERT OR IGNORE INTO {table}({quoted}) VALUES ({placeholders})"
            )
            connection.executemany(
                sql,
                [tuple(encode(row.get(column)) for column in columns) for row in rows],
            )
            return len(rows)

        return await self._write(operation)

    async def export_rows(self, *, table: str, columns: Sequence[str]) -> list[dict[str, Any]]:
        allowed = {
            "conversations",
            "conversation_message_index",
            "session_entries",
            "session_lanes",
            "session_records",
            "agent_runs",
            "run_events",
            "agent_plan_steps",
            "agent_attempts",
            "agent_checkpoints",
            "cowork_run_configs",
            "tool_invocations",
            "session_roots",
            "capability_grants",
            "artifacts",
            "cowork_attachments",
            "cowork_schedules",
            "cowork_inbox_items",
            "cowork_steering_messages",
            "cowork_teams",
            "cowork_team_workers",
            "cowork_team_worker_sessions",
            "cowork_board_tasks",
            "cowork_memories",
        }
        if table not in allowed or not columns:
            raise ValueError("不允许导出该 Cowork 表")

        def operation(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            available = {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not set(columns).issubset(available):
                raise ValueError(f"{table} 双读列与 SQLite schema 不一致")
            rows = connection.execute(
                f"SELECT {', '.join(columns)} FROM {table} ORDER BY {', '.join(columns)}"
            ).fetchall()
            return [dict(row) for row in rows]

        return await self._read(operation)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    async def _read(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        def run() -> T:
            connection = self._connect()
            try:
                return operation(connection)
            finally:
                connection.close()

        return await asyncio.to_thread(run)

    async def _write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        async with self._write_lock:

            def run() -> T:
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    value = operation(connection)
                    connection.commit()
                    return value
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    connection.close()

            return await asyncio.to_thread(run)

    async def create_conversation(self, *, title: str | None = None) -> UUID:
        conversation_id = uuid7()
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> UUID:
            connection.execute(
                """
                INSERT INTO conversations(id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(conversation_id), title, timestamp, timestamp),
            )
            return conversation_id

        return await self._write(operation)

    async def conversation_exists(self, conversation_id: UUID) -> bool:
        return await self._read(
            lambda connection: (
                connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (str(conversation_id),)
                ).fetchone()
                is not None
            )
        )

    async def compare_and_set_conversation_title(
        self,
        *,
        conversation_id: UUID,
        expected_title: str | None,
        title: str,
    ) -> bool:
        normalized = " ".join(title.split())[:120]
        if not normalized:
            raise ValueError("会话标题不能为空")

        def operation(connection: sqlite3.Connection) -> bool:
            if expected_title is None:
                cursor = connection.execute(
                    """UPDATE conversations SET title = ?
                       WHERE id = ? AND title IS NULL""",
                    (normalized, str(conversation_id)),
                )
            else:
                cursor = connection.execute(
                    """UPDATE conversations SET title = ?
                       WHERE id = ? AND title = ?""",
                    (normalized, str(conversation_id), expected_title),
                )
            return cursor.rowcount > 0

        return await self._write(operation)

    async def allocate_message(
        self,
        *,
        record_id: UUID,
        conversation_id: UUID,
        role: str,
        status: str,
        run_id: UUID | None,
        title_source: str,
    ) -> int:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> int:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM conversation_message_index WHERE conversation_id = ?",
                (str(conversation_id),),
            ).fetchone()
            assert row is not None
            seq = int(row["seq"])
            connection.execute(
                """INSERT INTO conversation_message_index(
                       record_id, conversation_id, seq, role, status, run_id,
                       content_preview, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(record_id),
                    str(conversation_id),
                    seq,
                    role,
                    status,
                    None if run_id is None else str(run_id),
                    title_source[:160],
                    timestamp,
                ),
            )
            if role == "user":
                connection.execute(
                    """UPDATE conversations SET
                           title = CASE WHEN title IS NULL THEN ? ELSE title END,
                           updated_at = ? WHERE id = ?""",
                    (title_source.strip()[:80], timestamp, str(conversation_id)),
                )
            else:
                connection.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (timestamp, str(conversation_id)),
                )
            return seq

        return await self._write(operation)

    async def list_streaming_message_ids(self, *, run_id: UUID) -> list[UUID]:
        return await self._read(
            lambda connection: [
                UUID(row["record_id"])
                for row in connection.execute(
                    """SELECT record_id FROM conversation_message_index
                       WHERE run_id = ? AND status = 'streaming' ORDER BY seq""",
                    (str(run_id),),
                ).fetchall()
            ]
        )

    async def update_message_status(
        self,
        *,
        record_id: UUID,
        status: str,
        content_preview: str | None = None,
    ) -> None:
        await self._write(
            lambda connection: connection.execute(
                """UPDATE conversation_message_index
                   SET status = ?, content_preview = COALESCE(?, content_preview)
                   WHERE record_id = ?""",
                (
                    status,
                    None if content_preview is None else content_preview[:160],
                    str(record_id),
                ),
            )
        )

    async def get_message_conversation_id(self, *, record_id: UUID) -> UUID | None:
        value = await self._read(
            lambda connection: (
                None
                if (
                    row := connection.execute(
                        "SELECT conversation_id FROM conversation_message_index WHERE record_id = ?",
                        (str(record_id),),
                    ).fetchone()
                )
                is None
                else str(row["conversation_id"])
            )
        )
        return None if value is None else UUID(value)

    async def list_conversation_metadata(
        self,
        *,
        conversation_id: UUID | None = None,
        archived: bool | None = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("conversation limit 必须位于 1 到 200")

        def operation(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            clauses: list[str] = []
            parameters: list[Any] = []
            if conversation_id is not None:
                clauses.append("id = ?")
                parameters.append(str(conversation_id))
            if archived is not None:
                clauses.append("archived_at IS NOT NULL" if archived else "archived_at IS NULL")
            where = "" if not clauses else "WHERE " + " AND ".join(clauses)
            parameters.append(limit)
            rows = connection.execute(
                f"""WITH RECURSIVE main_branch(
                           conversation_id, id, parent_id, kind, payload, path
                       ) AS (
                           SELECT lanes.conversation_id, entries.id, entries.parent_id,
                                  entries.kind, entries.payload, ',' || entries.id || ','
                           FROM session_lanes AS lanes
                           JOIN session_entries AS entries ON entries.id = lanes.head_entry_id
                           WHERE lanes.name = 'main'
                           UNION ALL
                           SELECT parent.conversation_id, parent.id, parent.parent_id,
                                  parent.kind, parent.payload, child.path || parent.id || ','
                           FROM session_entries AS parent
                           JOIN main_branch AS child ON child.parent_id = parent.id
                           WHERE parent.conversation_id = child.conversation_id
                             AND instr(child.path, ',' || parent.id || ',') = 0
                       )
                    SELECT conversations.*,
                           (SELECT COUNT(*) FROM conversation_message_index AS messages
                            WHERE messages.conversation_id = conversations.id
                              AND messages.role IN ('user', 'assistant')
                              AND (
                                  NOT EXISTS (
                                      SELECT 1 FROM main_branch AS branch
                                      WHERE branch.conversation_id = conversations.id
                                  )
                                  OR messages.record_id IN (
                                      SELECT json_extract(branch.payload, '$.record_id')
                                      FROM main_branch AS branch
                                      WHERE branch.conversation_id = conversations.id
                                        AND branch.kind = 'message'
                                  )
                              )) AS message_count,
                           (SELECT messages.content_preview
                            FROM conversation_message_index AS messages
                            WHERE messages.conversation_id = conversations.id
                              AND messages.role IN ('user', 'assistant')
                              AND (
                                  NOT EXISTS (
                                      SELECT 1 FROM main_branch AS branch
                                      WHERE branch.conversation_id = conversations.id
                                  )
                                  OR messages.record_id IN (
                                      SELECT json_extract(branch.payload, '$.record_id')
                                      FROM main_branch AS branch
                                      WHERE branch.conversation_id = conversations.id
                                        AND branch.kind = 'message'
                                  )
                              )
                              AND messages.content_preview IS NOT NULL
                              AND messages.content_preview <> ''
                            ORDER BY messages.seq DESC LIMIT 1) AS latest_message,
                           (SELECT messages.created_at
                            FROM conversation_message_index AS messages
                            WHERE messages.conversation_id = conversations.id
                              AND messages.role IN ('user', 'assistant')
                              AND (
                                  NOT EXISTS (
                                      SELECT 1 FROM main_branch AS branch
                                      WHERE branch.conversation_id = conversations.id
                                  )
                                  OR messages.record_id IN (
                                      SELECT json_extract(branch.payload, '$.record_id')
                                      FROM main_branch AS branch
                                      WHERE branch.conversation_id = conversations.id
                                        AND branch.kind = 'message'
                                  )
                              )
                            ORDER BY messages.seq DESC LIMIT 1) AS last_message_at,
                           (SELECT runs.id FROM agent_runs AS runs
                            WHERE runs.conversation_id = conversations.id
                              AND runs.status IN (
                                  'initializing','queued','executing','waiting_human','sleeping'
                              )
                            ORDER BY runs.created_at DESC, runs.id DESC LIMIT 1) AS active_run_id
                    FROM conversations {where}
                    ORDER BY updated_at DESC, id DESC LIMIT ?""",
                tuple(parameters),
            ).fetchall()
            return [dict(row) for row in rows]

        return await self._read(operation)

    async def set_conversation_archived(self, *, conversation_id: UUID, archived: bool) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            active = connection.execute(
                """SELECT 1 FROM agent_runs WHERE conversation_id = ?
                   AND status NOT IN ('done','partial','failed','cancelled','budget_exceeded') LIMIT 1""",
                (str(conversation_id),),
            ).fetchone()
            if active is not None:
                raise ConversationBusyError("会话仍有任务在运行")
            timestamp = _iso()
            return (
                connection.execute(
                    """UPDATE conversations SET archived_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (timestamp if archived else None, timestamp, str(conversation_id)),
                ).rowcount
                == 1
            )

        return await self._write(operation)

    async def update_conversation_runtime(
        self,
        *,
        conversation_id: UUID,
        provider_profile_id: UUID | None,
        model_override: str | None,
        unattended: bool,
        approval_mode: ApprovalMode,
        persona_name: str,
    ) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            previous = connection.execute(
                "SELECT provider_profile_id, model_override FROM conversations WHERE id = ?",
                (str(conversation_id),),
            ).fetchone()
            changed = (
                connection.execute(
                    """UPDATE conversations SET provider_profile_id = ?, model_override = ?,
                          unattended = ?, approval_mode = ?, persona_name = ?, updated_at = ?
                       WHERE id = ?""",
                    (
                        None if provider_profile_id is None else str(provider_profile_id),
                        model_override,
                        int(unattended),
                        approval_mode,
                        persona_name,
                        _iso(),
                        str(conversation_id),
                    ),
                ).rowcount
                == 1
            )
            if (
                changed
                and previous is not None
                and (
                    previous["provider_profile_id"]
                    != (None if provider_profile_id is None else str(provider_profile_id))
                    or previous["model_override"] != model_override
                )
            ):
                self._append_session_entry_transaction(
                    connection,
                    conversation_id=conversation_id,
                    kind="model_change",
                    payload={
                        "provider_profile_id": (
                            None if provider_profile_id is None else str(provider_profile_id)
                        ),
                        "model_override": model_override,
                    },
                    entry_id=None,
                    parent_id=None,
                    lane="main",
                )
            return changed

        return await self._write(operation)

    @staticmethod
    def _session_entry(row: sqlite3.Row) -> SessionEntry:
        payload = json.loads(str(row["payload"]))
        if not isinstance(payload, dict):
            raise ValueError("session entry payload 不是对象")
        return SessionEntry(
            id=str(row["id"]),
            conversation_id=UUID(str(row["conversation_id"])),
            parent_id=None if row["parent_id"] is None else str(row["parent_id"]),
            seq=int(row["seq"]),
            kind=cast("SessionEntryKind", str(row["kind"])),
            payload=payload,
            created_at=str(row["created_at"]),
        )

    def _append_session_entry_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        conversation_id: UUID,
        kind: SessionEntryKind,
        payload: dict[str, Any],
        entry_id: str | None,
        parent_id: str | None,
        lane: str,
    ) -> SessionEntry:
        if not lane.strip() or len(lane) > 80:
            raise ValueError("session lane 名称长度必须位于 1 到 80")
        if (
            connection.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (str(conversation_id),)
            ).fetchone()
            is None
        ):
            raise ConversationNotFoundError(str(conversation_id))
        resolved_id = entry_id or str(uuid7())
        encoded_payload = canonical_json(payload)
        existing = connection.execute(
            "SELECT * FROM session_entries WHERE id = ?", (resolved_id,)
        ).fetchone()
        if existing is not None:
            record = self._session_entry(existing)
            if (
                record.conversation_id != conversation_id
                or record.kind != kind
                or canonical_json(record.payload) != encoded_payload
            ):
                raise ValueError(f"session entry id 冲突: {resolved_id}")
            return record
        if parent_id is None:
            lane_row = connection.execute(
                "SELECT head_entry_id FROM session_lanes WHERE conversation_id = ? AND name = ?",
                (str(conversation_id), lane),
            ).fetchone()
            parent_id = None if lane_row is None else lane_row["head_entry_id"]
        if parent_id is not None:
            parent = connection.execute(
                "SELECT conversation_id FROM session_entries WHERE id = ?", (parent_id,)
            ).fetchone()
            if parent is None or str(parent["conversation_id"]) != str(conversation_id):
                raise ValueError("session entry parent 不存在或属于其他会话")
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM session_entries WHERE conversation_id = ?",
            (str(conversation_id),),
        ).fetchone()
        assert row is not None
        seq = int(row["seq"])
        timestamp = _iso()
        connection.execute(
            """INSERT INTO session_entries(id, conversation_id, parent_id, seq, kind, payload, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                resolved_id,
                str(conversation_id),
                parent_id,
                seq,
                kind,
                encoded_payload,
                timestamp,
            ),
        )
        connection.execute(
            """INSERT INTO session_lanes(conversation_id, name, head_entry_id, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(conversation_id, name) DO UPDATE SET
                   head_entry_id = excluded.head_entry_id, updated_at = excluded.updated_at""",
            (str(conversation_id), lane, resolved_id, timestamp),
        )
        inserted = connection.execute(
            "SELECT * FROM session_entries WHERE id = ?", (resolved_id,)
        ).fetchone()
        assert inserted is not None
        return self._session_entry(inserted)

    async def append_session_entry(
        self,
        *,
        conversation_id: UUID,
        kind: SessionEntryKind,
        payload: dict[str, Any],
        entry_id: str | None = None,
        parent_id: str | None = None,
        lane: str = "main",
    ) -> SessionEntry:
        return await self._write(
            lambda connection: self._append_session_entry_transaction(
                connection,
                conversation_id=conversation_id,
                kind=kind,
                payload=payload,
                entry_id=entry_id,
                parent_id=parent_id,
                lane=lane,
            )
        )

    async def list_session_entries(
        self,
        *,
        conversation_id: UUID,
        lane: str | None = None,
        limit: int = 1000,
    ) -> list[SessionEntry]:
        if not 1 <= limit <= 10_000:
            raise ValueError("session entry limit 必须位于 1 到 10000")

        def operation(connection: sqlite3.Connection) -> list[SessionEntry]:
            if lane is None:
                rows = connection.execute(
                    """SELECT * FROM session_entries WHERE conversation_id = ?
                       ORDER BY seq DESC LIMIT ?""",
                    (str(conversation_id), limit),
                ).fetchall()
                return list(reversed([self._session_entry(row) for row in rows]))
            head = connection.execute(
                "SELECT head_entry_id FROM session_lanes WHERE conversation_id = ? AND name = ?",
                (str(conversation_id), lane),
            ).fetchone()
            if head is None or head["head_entry_id"] is None:
                return []
            rows = connection.execute(
                """WITH RECURSIVE ancestry(
                       id, conversation_id, parent_id, seq, kind, payload, created_at, path
                   ) AS (
                       SELECT id, conversation_id, parent_id, seq, kind, payload, created_at,
                              ',' || id || ','
                       FROM session_entries WHERE id = ? AND conversation_id = ?
                       UNION ALL
                       SELECT parent.id, parent.conversation_id, parent.parent_id, parent.seq,
                              parent.kind, parent.payload, parent.created_at,
                              child.path || parent.id || ','
                       FROM session_entries AS parent
                       JOIN ancestry AS child ON child.parent_id = parent.id
                       WHERE parent.conversation_id = ?
                         AND instr(child.path, ',' || parent.id || ',') = 0
                   )
                   SELECT id, conversation_id, parent_id, seq, kind, payload, created_at
                   FROM ancestry ORDER BY seq DESC LIMIT ?""",
                (
                    str(head["head_entry_id"]),
                    str(conversation_id),
                    str(conversation_id),
                    limit + 1,
                ),
            ).fetchall()
            if not rows:
                raise ValueError("session lane head 已损坏")
            records = list(reversed([self._session_entry(row) for row in rows]))
            if len(records) <= limit and records[0].parent_id is not None:
                raise ValueError("session lane parent 链已损坏或成环")
            return records[-limit:]

        return await self._read(operation)

    async def move_session_lane(
        self,
        *,
        conversation_id: UUID,
        lane: str,
        entry_id: str | None,
    ) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            if not lane.strip() or len(lane) > 80:
                raise ValueError("session lane 名称长度必须位于 1 到 80")
            if (
                connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (str(conversation_id),)
                ).fetchone()
                is None
            ):
                raise ConversationNotFoundError(str(conversation_id))
            if entry_id is not None:
                row = connection.execute(
                    "SELECT conversation_id FROM session_entries WHERE id = ?", (entry_id,)
                ).fetchone()
                if row is None or str(row["conversation_id"]) != str(conversation_id):
                    return False
            connection.execute(
                """INSERT INTO session_lanes(conversation_id, name, head_entry_id, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(conversation_id, name) DO UPDATE SET
                       head_entry_id = excluded.head_entry_id, updated_at = excluded.updated_at""",
                (str(conversation_id), lane, entry_id, _iso()),
            )
            return True

        return await self._write(operation)

    async def navigate_session_lane(
        self,
        *,
        conversation_id: UUID,
        lane: str,
        target_entry_id: str | None,
        expected_head_entry_id: str | None,
        abandoned_lane: str,
        branch_summary_payload: dict[str, Any] | None = None,
    ) -> SessionLaneNavigation:
        """Atomically preserve the lane being left and move its active pointer.

        The expected-head compare closes the race between building an abandoned-branch preview
        and committing the navigation.  Runs and navigation are mutually exclusive: otherwise a
        worker could append an assistant result to a branch after the UI has left it.
        """

        def operation(connection: sqlite3.Connection) -> SessionLaneNavigation:
            for value, label in ((lane, "lane"), (abandoned_lane, "abandoned_lane")):
                if not value.strip() or len(value) > 80:
                    raise ValueError(f"session {label} 名称长度必须位于 1 到 80")
            if lane == abandoned_lane:
                raise ValueError("abandoned lane 不能与当前 lane 同名")
            if (
                connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (str(conversation_id),)
                ).fetchone()
                is None
            ):
                raise ConversationNotFoundError(str(conversation_id))
            active = connection.execute(
                """SELECT 1 FROM agent_runs WHERE conversation_id = ? AND status IN (
                       'initializing','queued','executing','waiting_human','sleeping'
                   ) LIMIT 1""",
                (str(conversation_id),),
            ).fetchone()
            if active is not None:
                raise ConversationBusyError("会话仍有任务在运行，不能移动会话分支")
            lane_row = connection.execute(
                "SELECT head_entry_id FROM session_lanes WHERE conversation_id = ? AND name = ?",
                (str(conversation_id), lane),
            ).fetchone()
            previous_head = None if lane_row is None else lane_row["head_entry_id"]
            if previous_head != expected_head_entry_id:
                raise ConversationBusyError("会话分支已被其他操作移动，请刷新后重试")
            if target_entry_id is not None:
                target = connection.execute(
                    "SELECT conversation_id FROM session_entries WHERE id = ?",
                    (target_entry_id,),
                ).fetchone()
                if target is None or str(target["conversation_id"]) != str(conversation_id):
                    raise ValueError("目标 session entry 不存在或属于其他会话")
            if previous_head == target_entry_id:
                return SessionLaneNavigation(
                    conversation_id=conversation_id,
                    lane=lane,
                    previous_head_entry_id=previous_head,
                    current_head_entry_id=target_entry_id,
                    abandoned_lane=None,
                    branch_summary_entry_id=None,
                )

            timestamp = _iso()
            preserved_lane: str | None = None
            summary_id: str | None = None
            if previous_head is not None:
                collision = connection.execute(
                    "SELECT 1 FROM session_lanes WHERE conversation_id = ? AND name = ?",
                    (str(conversation_id), abandoned_lane),
                ).fetchone()
                if collision is not None:
                    raise ValueError("abandoned lane 名称已存在")
                connection.execute(
                    """INSERT INTO session_lanes(
                           conversation_id, name, head_entry_id, updated_at
                       ) VALUES (?, ?, ?, ?)""",
                    (str(conversation_id), abandoned_lane, previous_head, timestamp),
                )
                preserved_lane = abandoned_lane
                if branch_summary_payload is not None:
                    summary = self._append_session_entry_transaction(
                        connection,
                        conversation_id=conversation_id,
                        kind="branch_summary",
                        payload=branch_summary_payload,
                        entry_id=None,
                        parent_id=str(previous_head),
                        lane=abandoned_lane,
                    )
                    summary_id = summary.id
            connection.execute(
                """INSERT INTO session_lanes(conversation_id, name, head_entry_id, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(conversation_id, name) DO UPDATE SET
                       head_entry_id = excluded.head_entry_id, updated_at = excluded.updated_at""",
                (str(conversation_id), lane, target_entry_id, timestamp),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (timestamp, str(conversation_id)),
            )
            return SessionLaneNavigation(
                conversation_id=conversation_id,
                lane=lane,
                previous_head_entry_id=None if previous_head is None else str(previous_head),
                current_head_entry_id=target_entry_id,
                abandoned_lane=preserved_lane,
                branch_summary_entry_id=summary_id,
            )

        return await self._write(operation)

    @staticmethod
    def _session_record(row: sqlite3.Row) -> SessionRecord:
        payload = json.loads(str(row["payload"]))
        if not isinstance(payload, dict):
            raise ValueError("session record payload 不是对象")
        created_at = _datetime(row["created_at"])
        if created_at is None:
            raise ValueError("session record created_at 无效")
        return SessionRecord(
            id=str(row["id"]),
            run_id=UUID(str(row["run_id"])),
            seq=int(row["seq"]),
            kind=cast("SessionRecordKind", str(row["kind"])),
            operation_id=str(row["operation_id"]),
            phase=cast("SessionRecordPhase", str(row["phase"])),
            payload=payload,
            created_at=created_at,
        )

    def _append_session_record_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: UUID,
        kind: SessionRecordKind,
        operation_id: str,
        phase: SessionRecordPhase,
        payload: dict[str, Any],
        record_id: str | None = None,
    ) -> SessionRecord:
        if not operation_id.strip() or len(operation_id) > 120:
            raise ValueError("session record operation_id 长度必须位于 1 到 120")
        encoded = canonical_json(payload)
        resolved_id = record_id or str(uuid7())
        existing = connection.execute(
            """SELECT * FROM session_records
               WHERE run_id = ? AND operation_id = ? AND phase = ?""",
            (str(run_id), operation_id, phase),
        ).fetchone()
        if existing is not None:
            record = self._session_record(existing)
            if record.kind != kind or canonical_json(record.payload) != encoded:
                raise ValueError(f"session record operation/phase 冲突: {operation_id}/{phase}")
            return record
        if (
            connection.execute(
                "SELECT 1 FROM agent_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
            is None
        ):
            raise RunNotFoundError(str(run_id))
        seq = int(
            connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM session_records WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()[0]
        )
        connection.execute(
            """INSERT INTO session_records(
                   id, run_id, seq, kind, operation_id, phase, payload, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                resolved_id,
                str(run_id),
                seq,
                kind,
                operation_id,
                phase,
                encoded,
                _iso(),
            ),
        )
        row = connection.execute(
            "SELECT * FROM session_records WHERE id = ?", (resolved_id,)
        ).fetchone()
        assert row is not None
        return self._session_record(row)

    async def append_session_record(
        self,
        *,
        run_id: UUID,
        kind: SessionRecordKind,
        operation_id: str,
        phase: SessionRecordPhase,
        payload: dict[str, Any],
        record_id: str | None = None,
    ) -> SessionRecord:
        return await self._write(
            lambda connection: self._append_session_record_transaction(
                connection,
                run_id=run_id,
                kind=kind,
                operation_id=operation_id,
                phase=phase,
                payload=payload,
                record_id=record_id,
            )
        )

    async def list_session_records(self, *, run_id: UUID) -> list[SessionRecord]:
        return await self._read(
            lambda connection: [
                self._session_record(row)
                for row in connection.execute(
                    "SELECT * FROM session_records WHERE run_id = ? ORDER BY seq",
                    (str(run_id),),
                ).fetchall()
            ]
        )

    async def list_conversation_skill_mutes(self, *, conversation_id: UUID) -> frozenset[str]:
        def operation(connection: sqlite3.Connection) -> frozenset[str]:
            if (
                connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (str(conversation_id),)
                ).fetchone()
                is None
            ):
                raise ConversationNotFoundError(str(conversation_id))
            return frozenset(
                str(row["skill_name"])
                for row in connection.execute(
                    """SELECT skill_name FROM cowork_conversation_skill_mutes
                       WHERE conversation_id = ? ORDER BY skill_name""",
                    (str(conversation_id),),
                ).fetchall()
            )

        return await self._read(operation)

    async def set_conversation_skill_muted(
        self,
        *,
        conversation_id: UUID,
        skill_name: str,
        muted: bool,
    ) -> frozenset[str]:
        def operation(connection: sqlite3.Connection) -> frozenset[str]:
            if (
                connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (str(conversation_id),)
                ).fetchone()
                is None
            ):
                raise ConversationNotFoundError(str(conversation_id))
            active = connection.execute(
                """SELECT 1 FROM agent_runs WHERE conversation_id = ?
                   AND status IN ('initializing','queued','executing','waiting_human','sleeping')
                   LIMIT 1""",
                (str(conversation_id),),
            ).fetchone()
            if active is not None:
                raise ConversationBusyError("会话仍有任务在运行，Skill 设置只能在两次运行之间修改")
            if muted:
                connection.execute(
                    """INSERT INTO cowork_conversation_skill_mutes(
                           conversation_id, skill_name, created_at
                       ) VALUES (?, ?, ?) ON CONFLICT(conversation_id, skill_name) DO NOTHING""",
                    (str(conversation_id), skill_name, _iso()),
                )
            else:
                connection.execute(
                    """DELETE FROM cowork_conversation_skill_mutes
                       WHERE conversation_id = ? AND skill_name = ?""",
                    (str(conversation_id), skill_name),
                )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (_iso(), str(conversation_id)),
            )
            return frozenset(
                str(row["skill_name"])
                for row in connection.execute(
                    """SELECT skill_name FROM cowork_conversation_skill_mutes
                       WHERE conversation_id = ? ORDER BY skill_name""",
                    (str(conversation_id),),
                ).fetchall()
            )

        return await self._write(operation)

    async def delete_conversation(self, *, conversation_id: UUID) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            leased_worker = connection.execute(
                """SELECT 1 FROM agent_runs WHERE conversation_id = ?
                   AND status NOT IN ('done','partial','failed','cancelled','budget_exceeded')
                   AND worker_id IS NOT NULL AND lease_until IS NOT NULL
                   AND lease_until > ? LIMIT 1""",
                (str(conversation_id), _iso()),
            ).fetchone()
            if leased_worker is not None:
                raise ConversationBusyError("会话任务正在执行")
            # v20 新库有 ON DELETE CASCADE；旧库无法原地补 FK，仍在同一事务显式 purge。
            # 先清空再删除，使未来若 DELETE 被 trigger/扩展改成归档，也不会残留完整消息。
            connection.execute(
                "UPDATE memory_extraction_jobs SET content = '' WHERE conversation_id = ?",
                (str(conversation_id),),
            )
            connection.execute(
                "DELETE FROM memory_extraction_jobs WHERE conversation_id = ?",
                (str(conversation_id),),
            )
            team_ids = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM cowork_teams WHERE lead_conversation_id = ?",
                    (str(conversation_id),),
                ).fetchall()
            ]
            for team_id in team_ids:
                connection.execute(
                    "INSERT INTO cowork_team_event_purge_guards(team_id) VALUES (?)",
                    (team_id,),
                )
                # outbox 先删：它以 event_id FK 保护未决投递；其余无 FK 的 Team audit
                # projection 也必须随用户删除一起清除，避免 task/path/report 摘要 orphan。
                connection.execute(
                    "DELETE FROM cowork_team_wake_outbox WHERE team_id = ?", (team_id,)
                )
                connection.execute("DELETE FROM cowork_team_events WHERE team_id = ?", (team_id,))
                connection.execute(
                    "DELETE FROM cowork_team_event_heads WHERE team_id = ?", (team_id,)
                )
                connection.execute(
                    "DELETE FROM cowork_team_event_cursors WHERE team_id = ?", (team_id,)
                )
                connection.execute(
                    "DELETE FROM cowork_team_event_projection_summaries WHERE team_id = ?",
                    (team_id,),
                )
                connection.execute(
                    "DELETE FROM cowork_team_event_purge_guards WHERE team_id = ?", (team_id,)
                )
            # session_entries uses a self-referencing RESTRICT edge so callers cannot
            # accidentally orphan a branch by deleting an individual parent.  A whole
            # conversation purge is different: detach the private tree in this same
            # transaction, then remove lanes and entries before deleting the owner.
            connection.execute(
                "DELETE FROM session_lanes WHERE conversation_id = ?",
                (str(conversation_id),),
            )
            connection.execute(
                "UPDATE session_entries SET parent_id = NULL WHERE conversation_id = ?",
                (str(conversation_id),),
            )
            connection.execute(
                "DELETE FROM session_entries WHERE conversation_id = ?",
                (str(conversation_id),),
            )
            return (
                connection.execute(
                    "DELETE FROM conversations WHERE id = ?", (str(conversation_id),)
                ).rowcount
                == 1
            )

        return await self._write(operation)

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
        source_wake_id: UUID | None = None,
    ) -> RunRecord:
        if not goal.strip():
            raise ValueError("run 目标不能为空")
        if not 1 <= retrieval_top_k <= 20:
            raise ValueError("retrieval_top_k 必须位于 1 到 20")
        run_id = uuid7()
        timestamp = _iso()
        initial_status = "initializing" if initializing else "queued"

        def operation(connection: sqlite3.Connection) -> RunRecord:
            if source_wake_id is not None:
                existing = connection.execute(
                    "SELECT * FROM agent_runs WHERE source_wake_id = ?",
                    (str(source_wake_id),),
                ).fetchone()
                if existing is not None:
                    return self._run_record(existing)
            connection.execute(
                """
                INSERT INTO agent_runs(
                    id, conversation_id, goal, status, budget_tokens, budget_calls,
                    budget_wall_ms, answer_mode, workflow_type, schedule_id, unattended, run_trigger,
                    retrieval_top_k, source_wake_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(run_id),
                    str(conversation_id),
                    goal.strip(),
                    initial_status,
                    budget_tokens,
                    budget_calls,
                    budget_wall_ms,
                    answer_mode,
                    workflow_type,
                    None if schedule_id is None else str(schedule_id),
                    int(unattended),
                    run_trigger,
                    retrieval_top_k,
                    None if source_wake_id is None else str(source_wake_id),
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
            assert row is not None
            return self._run_record(row)

        return await self._write(operation)

    @staticmethod
    def _persisted_checkpoint_state(
        connection: sqlite3.Connection,
        *,
        run_id: UUID,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """同步单行 RunConfig，并返回只含可变字段的 checkpoint state。"""

        run_config, checkpoint_state = split_cowork_state(state)
        if run_config is None:
            return checkpoint_state
        exists = connection.execute(
            "SELECT 1 FROM cowork_run_configs WHERE run_id = ?", (str(run_id),)
        ).fetchone()
        if exists is None:
            timestamp = _iso()
            connection.execute(
                """INSERT INTO cowork_run_configs(run_id, config, created_at, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (str(run_id), canonical_json(run_config), timestamp, timestamp),
            )
        return checkpoint_state

    @staticmethod
    def _update_run_config_transaction(
        connection: sqlite3.Connection,
        *,
        run_id: UUID,
        run_config: dict[str, Any],
    ) -> None:
        changed = connection.execute(
            """UPDATE cowork_run_configs SET config = ?, updated_at = ?
               WHERE run_id = ?""",
            (canonical_json(run_config), _iso(), str(run_id)),
        ).rowcount
        if changed != 1:
            raise LookupError(f"Cowork RunConfig 不存在: {run_id}")

    @staticmethod
    def _run_config_transaction(
        connection: sqlite3.Connection, *, run_id: UUID
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT config FROM cowork_run_configs WHERE run_id = ?", (str(run_id),)
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row["config"])
        if not isinstance(value, dict):  # pragma: no cover - 写入路径保证 object
            raise ValueError("Cowork RunConfig 不是 JSON object")
        return value

    @classmethod
    def _stored_checkpoint(
        cls, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> StoredCheckpoint:
        checkpoint_state = json.loads(row["state"])
        if not isinstance(checkpoint_state, dict):  # pragma: no cover - 写入路径保证 object
            raise ValueError("Cowork checkpoint state 不是 JSON object")
        run_id = UUID(row["run_id"])
        return StoredCheckpoint(
            run_id=run_id,
            checkpoint_id=str(row["checkpoint_id"]),
            parent_id=row["parent_id"],
            state=merge_cowork_state(
                checkpoint_state,
                cls._run_config_transaction(connection, run_id=run_id),
            ),
        )

    async def initialize_run(
        self,
        *,
        run_id: UUID,
        state: dict[str, Any],
        checkpoint_id: str,
        events: Sequence[RunEventDraft],
    ) -> tuple[RunRecord, StoredCheckpoint, list[RunEvent]]:
        """原子写入初始 checkpoint/event，并把 initializing run 变为 queued。

        兼容测试和离线 runner 直接创建的 queued run；它们没有并发 dispatcher，但仍复用
        同一份 checkpoint/event 事务语义。
        """

        checkpoint = StoredCheckpoint(run_id, checkpoint_id, None, state)

        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[RunRecord, StoredCheckpoint, list[RunEvent]]:
            row = connection.execute(
                "SELECT status FROM agent_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
            if row is None:
                raise LookupError(f"run 不存在: {run_id}")
            status = str(row["status"])
            if status not in {"initializing", "queued"}:
                raise RuntimeError(f"run 当前状态不能初始化: {status}")
            if connection.execute(
                "SELECT 1 FROM agent_checkpoints WHERE run_id = ? LIMIT 1", (str(run_id),)
            ).fetchone():
                raise RuntimeError("run 已存在初始 checkpoint")
            checkpoint_state = self._persisted_checkpoint_state(
                connection, run_id=run_id, state=state
            )
            connection.execute(
                """INSERT INTO agent_checkpoints(
                           run_id, checkpoint_id, parent_id, state, created_at
                       ) VALUES (?, ?, NULL, ?, ?)""",
                (str(run_id), checkpoint_id, canonical_json(checkpoint_state), _iso()),
            )
            stored_events = self._append_events_transaction(connection, run_id, events)
            if status == "initializing":
                changed = connection.execute(
                    """UPDATE agent_runs SET status = 'queued', updated_at = ?
                       WHERE id = ? AND status = 'initializing'""",
                    (_iso(), str(run_id)),
                ).rowcount
                if changed != 1:  # pragma: no cover - 同一 BEGIN IMMEDIATE 内不可并发漂移
                    raise RuntimeError("run 初始化状态发生并发漂移")
            refreshed = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
            assert refreshed is not None
            return self._run_record(refreshed), checkpoint, stored_events

        return await self._write(operation)

    async def get_run(self, run_id: UUID) -> RunRecord | None:
        def operation(connection: sqlite3.Connection) -> RunRecord | None:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
            return None if row is None else self._run_record(row)

        return await self._read(operation)

    async def get_runs(self, run_ids: Sequence[UUID]) -> list[RunRecord]:
        unique_ids = tuple(dict.fromkeys(run_ids))
        if not unique_ids:
            return []
        if len(unique_ids) > 500:
            raise ValueError("单次 run 批量读取上限为 500")

        def operation(connection: sqlite3.Connection) -> list[RunRecord]:
            placeholders = ", ".join("?" for _ in unique_ids)
            rows = connection.execute(
                f"SELECT * FROM agent_runs WHERE id IN ({placeholders})",
                tuple(str(run_id) for run_id in unique_ids),
            ).fetchall()
            return [self._run_record(row) for row in rows]

        return await self._read(operation)

    async def get_latest_run(self, *, conversation_id: UUID) -> RunRecord | None:
        def operation(connection: sqlite3.Connection) -> RunRecord | None:
            row = connection.execute(
                """SELECT * FROM agent_runs WHERE conversation_id = ?
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (str(conversation_id),),
            ).fetchone()
            return None if row is None else self._run_record(row)

        return await self._read(operation)

    async def list_queued_runs(self, *, limit: int = 100) -> list[RunRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("queued run limit 必须位于 1 到 1000")

        def operation(connection: sqlite3.Connection) -> list[RunRecord]:
            rows = connection.execute(
                "SELECT * FROM agent_runs WHERE status = 'queued' ORDER BY created_at, id LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._run_record(row) for row in rows]

        return await self._read(operation)

    async def conversation_has_active_run(self, *, conversation_id: UUID) -> bool:
        return await self._read(
            lambda connection: (
                connection.execute(
                    """SELECT 1 FROM agent_runs WHERE conversation_id = ?
                   AND status IN (
                       'initializing','queued','executing','waiting_human','sleeping'
                   ) LIMIT 1""",
                    (str(conversation_id),),
                ).fetchone()
                is not None
            )
        )

    async def claim_run(self, *, run_id: UUID, worker_id: str, lease_s: int) -> RunRecord | None:
        if lease_s <= 0:
            raise ValueError("run lease 必须大于 0 秒")
        now = _now()
        lease_until = _iso(now + timedelta(seconds=lease_s))

        def operation(connection: sqlite3.Connection) -> RunRecord | None:
            changed = connection.execute(
                """
                UPDATE agent_runs
                SET status = 'executing', worker_id = ?, lease_until = ?, heartbeat_at = ?,
                    started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE id = ? AND status = 'queued' AND cancel_requested_at IS NULL
                """,
                (worker_id, lease_until, _iso(now), _iso(now), _iso(now), str(run_id)),
            ).rowcount
            if changed != 1:
                return None
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
            assert row is not None
            return self._run_record(row)

        return await self._write(operation)

    async def renew_run_lease(self, *, run_id: UUID, worker_id: str, lease_s: int) -> bool:
        now = _now()

        def operation(connection: sqlite3.Connection) -> bool:
            changed = connection.execute(
                """
                UPDATE agent_runs SET lease_until = ?, heartbeat_at = ?, updated_at = ?
                WHERE id = ? AND worker_id = ? AND status = 'executing'
                  AND cancel_requested_at IS NULL
                """,
                (
                    _iso(now + timedelta(seconds=lease_s)),
                    _iso(now),
                    _iso(now),
                    str(run_id),
                    worker_id,
                ),
            ).rowcount
            return changed == 1

        return await self._write(operation)

    async def append_events(
        self, *, run_id: UUID, events: Sequence[RunEventDraft]
    ) -> list[RunEvent]:
        if not events:
            return []

        def operation(connection: sqlite3.Connection) -> list[RunEvent]:
            return self._append_events_transaction(connection, run_id, events)

        return await self._write(operation)

    def _append_events_transaction(
        self,
        connection: sqlite3.Connection,
        run_id: UUID,
        events: Sequence[RunEventDraft],
    ) -> list[RunEvent]:
        if not events:
            return []
        row = connection.execute(
            "SELECT next_seq FROM agent_runs WHERE id = ?", (str(run_id),)
        ).fetchone()
        if row is None:
            raise LookupError(f"run 不存在: {run_id}")
        first_seq = int(row["next_seq"])
        created_at = _iso()
        timestamp = cast(datetime, _datetime(created_at))
        validated_events = [run_event(event_type, payload) for event_type, payload in events]
        encoded = [
            (
                str(run_id),
                first_seq + offset,
                event_type,
                canonical_json(dict(payload)),
                created_at,
            )
            for offset, (event_type, payload) in enumerate(validated_events)
        ]
        connection.executemany(
            "INSERT INTO run_events(run_id, seq, type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            encoded,
        )
        output = [
            RunEvent(run_id, first_seq + offset, event_type, dict(payload), timestamp)
            for offset, (event_type, payload) in enumerate(validated_events)
        ]
        connection.execute(
            "UPDATE agent_runs SET next_seq = ?, updated_at = ? WHERE id = ?",
            (first_seq + len(events), created_at, str(run_id)),
        )
        return output

    async def list_events(
        self,
        *,
        run_id: UUID,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[RunEvent]:
        if after_seq < 0:
            raise ValueError("after_seq 不能为负数")
        if limit is not None and limit < 1:
            raise ValueError("event limit 必须为正")

        def operation(connection: sqlite3.Connection) -> list[RunEvent]:
            sql = "SELECT * FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq"
            parameters: tuple[object, ...] = (str(run_id), after_seq)
            if limit is not None:
                sql += " LIMIT ?"
                parameters = (*parameters, limit)
            rows = connection.execute(sql, parameters).fetchall()
            return [
                RunEvent(
                    run_id=UUID(row["run_id"]),
                    seq=int(row["seq"]),
                    type=cast("RunEventType", str(row["type"])),
                    payload=json.loads(row["payload"]),
                    created_at=cast(datetime, _datetime(row["created_at"])),
                )
                for row in rows
            ]

        return await self._read(operation)

    async def save_checkpoint(
        self,
        *,
        run_id: UUID,
        state: dict[str, Any],
        parent_id: str | None,
        checkpoint_id: str | None = None,
    ) -> StoredCheckpoint:
        checkpoint = StoredCheckpoint(run_id, checkpoint_id or str(uuid7()), parent_id, state)

        def operation(connection: sqlite3.Connection) -> StoredCheckpoint:
            checkpoint_state = self._persisted_checkpoint_state(
                connection, run_id=run_id, state=state
            )
            connection.execute(
                """
                INSERT INTO agent_checkpoints(run_id, checkpoint_id, parent_id, state, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(run_id),
                    checkpoint.checkpoint_id,
                    parent_id,
                    canonical_json(checkpoint_state),
                    _iso(),
                ),
            )
            return checkpoint

        return await self._write(operation)

    async def commit_checkpoint(
        self,
        *,
        run_id: UUID,
        state: dict[str, Any],
        parent_id: str | None,
        checkpoint_id: str,
        used_tokens: int,
        used_calls: int,
        events: Sequence[RunEventDraft],
        run_config: dict[str, Any] | None = None,
        worker_id: str | None = None,
        transition_to: Literal["queued", "waiting_human", "sleeping"] | None = None,
        wake_at: datetime | None = None,
    ) -> tuple[StoredCheckpoint, list[RunEvent]]:
        if used_tokens < 0 or used_calls < 0:
            raise ValueError("run 用量增量不能为负")
        if transition_to == "sleeping" and wake_at is None:
            raise ValueError("sleeping checkpoint 必须提供 wake_at")
        if transition_to != "sleeping" and wake_at is not None:
            raise ValueError("只有 sleeping checkpoint 可以提供 wake_at")
        checkpoint = StoredCheckpoint(run_id, checkpoint_id, parent_id, state)

        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[StoredCheckpoint, list[RunEvent]]:
            row = connection.execute(
                "SELECT status, worker_id, conversation_id FROM agent_runs WHERE id = ?",
                (str(run_id),),
            ).fetchone()
            if row is None:
                raise LookupError(f"run 不存在: {run_id}")
            if (
                transition_to != "queued"
                and worker_id is not None
                and (str(row["status"]) != "executing" or row["worker_id"] != worker_id)
            ):
                raise RuntimeError("当前 worker 已不再持有 run 租约")
            if transition_to is not None:
                if transition_to == "queued":
                    if worker_id is not None:
                        raise ValueError("恢复 waiting_human run 不应携带 worker_id")
                    changed = connection.execute(
                        """UPDATE agent_runs SET status = 'queued', worker_id = NULL,
                                  lease_until = NULL, heartbeat_at = NULL, wake_at = NULL,
                                  updated_at = ?
                           WHERE id = ? AND status = 'waiting_human'
                             AND cancel_requested_at IS NULL""",
                        (_iso(), str(run_id)),
                    ).rowcount
                else:
                    if worker_id is None:
                        raise ValueError("暂停 run 必须提供 worker_id")
                    changed = connection.execute(
                        """UPDATE agent_runs SET status = ?, worker_id = NULL,
                                  lease_until = NULL, heartbeat_at = NULL, wake_at = ?, updated_at = ?
                           WHERE id = ? AND worker_id = ? AND status = 'executing'
                             AND cancel_requested_at IS NULL""",
                        (
                            transition_to,
                            None if wake_at is None else _iso(wake_at),
                            _iso(),
                            str(run_id),
                            worker_id,
                        ),
                    ).rowcount
                if changed != 1:
                    raise RuntimeError("run 状态转换发生并发漂移")
            if used_tokens or used_calls:
                connection.execute(
                    """UPDATE agent_runs
                       SET used_tokens = used_tokens + ?, used_calls = used_calls + ?, updated_at = ?
                       WHERE id = ?""",
                    (used_tokens, used_calls, _iso(), str(run_id)),
                )
            checkpoint_state = self._persisted_checkpoint_state(
                connection, run_id=run_id, state=state
            )
            if run_config is not None:
                self._update_run_config_transaction(
                    connection, run_id=run_id, run_config=run_config
                )
            connection.execute(
                """INSERT INTO agent_checkpoints(
                           run_id, checkpoint_id, parent_id, state, created_at
                       ) VALUES (?, ?, ?, ?, ?)""",
                (
                    str(run_id),
                    checkpoint_id,
                    parent_id,
                    canonical_json(checkpoint_state),
                    _iso(),
                ),
            )
            state_status = state.get("status")
            if state_status in {"done", "failed", "cancelled", "budget_exceeded"}:
                compaction = state.get("compaction")
                compaction_payload = (
                    {
                        "revision": compaction.get("revision"),
                        "summary_upto": compaction.get("summary_upto"),
                        "turn_prefix_upto": compaction.get("turn_prefix_upto"),
                        "mode": compaction.get("last_mode"),
                    }
                    if isinstance(compaction, dict)
                    else None
                )
                self._append_session_entry_transaction(
                    connection,
                    conversation_id=UUID(str(row["conversation_id"])),
                    kind="custom",
                    payload={
                        "type": "checkpoint_ref",
                        "run_id": str(run_id),
                        "checkpoint_id": checkpoint_id,
                        "status": state_status,
                        "canonical_message_count": (
                            len(state["messages"])
                            if isinstance(state.get("messages"), list)
                            else None
                        ),
                        "compaction": compaction_payload,
                    },
                    entry_id=f"checkpoint-ref:{run_id}:{checkpoint_id}",
                    parent_id=None,
                    lane="main",
                )
            stored_events = self._append_events_transaction(connection, run_id, events)
            return checkpoint, stored_events

        return await self._write(operation)

    async def load_latest_checkpoint(self, *, run_id: UUID) -> StoredCheckpoint | None:
        def operation(connection: sqlite3.Connection) -> StoredCheckpoint | None:
            row = connection.execute(
                """
                SELECT * FROM agent_checkpoints WHERE run_id = ?
                ORDER BY created_at DESC, checkpoint_id DESC LIMIT 1
                """,
                (str(run_id),),
            ).fetchone()
            if row is None:
                return None
            return self._stored_checkpoint(connection, row)

        return await self._read(operation)

    async def load_checkpoint(
        self,
        *,
        run_id: UUID,
        checkpoint_id: str,
    ) -> StoredCheckpoint | None:
        def operation(connection: sqlite3.Connection) -> StoredCheckpoint | None:
            row = connection.execute(
                """SELECT * FROM agent_checkpoints
                   WHERE run_id = ? AND checkpoint_id = ?""",
                (str(run_id), checkpoint_id),
            ).fetchone()
            return None if row is None else self._stored_checkpoint(connection, row)

        return await self._read(operation)

    async def load_previous_checkpoint(self, *, run_id: UUID) -> StoredCheckpoint | None:
        def operation(connection: sqlite3.Connection) -> StoredCheckpoint | None:
            current = connection.execute(
                "SELECT conversation_id, created_at FROM agent_runs WHERE id = ?",
                (str(run_id),),
            ).fetchone()
            if current is None:
                return None
            row = connection.execute(
                """SELECT checkpoints.*
                   FROM agent_runs AS runs
                   JOIN agent_checkpoints AS checkpoints ON checkpoints.run_id = runs.id
                   WHERE runs.conversation_id = ? AND runs.id <> ?
                     AND runs.workflow_type = 'cowork'
                     AND runs.status IN ('done','partial','failed','cancelled','budget_exceeded')
                     AND runs.created_at < ?
                   ORDER BY runs.created_at DESC, checkpoints.checkpoint_id DESC LIMIT 1""",
                (current["conversation_id"], str(run_id), current["created_at"]),
            ).fetchone()
            if row is None:
                return None
            return self._stored_checkpoint(connection, row)

        return await self._read(operation)

    async def load_run_config(self, *, run_id: UUID) -> dict[str, Any] | None:
        return await self._read(
            lambda connection: self._run_config_transaction(connection, run_id=run_id)
        )

    async def acquire_invocation(
        self,
        *,
        run_id: UUID,
        plan_step_id: UUID,
        tool_name: str,
        args: dict[str, Any],
        worker_id: str,
        lease_s: int,
    ) -> InvocationLease:
        key, args_hash = invocation_identity(
            run_id=run_id,
            plan_step_id=plan_step_id,
            tool_name=tool_name,
            args=args,
        )
        now = _now()
        lease_until = _iso(now + timedelta(seconds=lease_s))

        def operation(connection: sqlite3.Connection) -> InvocationLease | None:
            existing = connection.execute(
                "SELECT * FROM tool_invocations WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO tool_invocations(
                        idempotency_key, run_id, plan_step_id, tool_name, args_hash,
                        status, lease_owner, lease_until, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'in_flight', ?, ?, ?, ?)
                    """,
                    (
                        key,
                        str(run_id),
                        str(plan_step_id),
                        tool_name,
                        args_hash,
                        worker_id,
                        lease_until,
                        _iso(now),
                        _iso(now),
                    ),
                )
                return InvocationLease(key, acquired=True)
            if str(existing["args_hash"]) != args_hash:
                raise RuntimeError("幂等键碰撞：已有调用的参数摘要不同")
            status = str(existing["status"])
            if status == "outcome_unknown":
                raise InvocationOutcomeUnknownError()
            expired = status == "in_flight" and str(existing["lease_until"] or "") < _iso(now)
            if expired:
                # A dead worker may have crashed before the effect, during it, or after it.
                # Reclaiming this lease would silently duplicate the latter two cases.  Persist
                # the uncertainty in the same transaction, then raise only after commit.
                timestamp = _iso(now)
                connection.execute(
                    """
                    UPDATE tool_invocations
                    SET status = 'outcome_unknown', result = ?, effect_ref = NULL,
                        lease_owner = NULL, lease_until = NULL, completed_at = ?, updated_at = ?
                    WHERE idempotency_key = ? AND status = 'in_flight'
                    """,
                    (
                        canonical_json(
                            {
                                "outcome": "unknown",
                                "reason": "effect_may_have_completed_before_lease_expiry",
                            }
                        ),
                        timestamp,
                        timestamp,
                        key,
                    ),
                )
                return None
            if status == "failed":
                connection.execute(
                    """
                    UPDATE tool_invocations
                    SET status = 'in_flight', lease_owner = ?, lease_until = ?,
                        retry_count = retry_count + 1, result = NULL, completed_at = NULL,
                        updated_at = ? WHERE idempotency_key = ?
                    """,
                    (worker_id, lease_until, _iso(now), key),
                )
                return InvocationLease(key, acquired=True)
            if status == "succeeded":
                result = None if existing["result"] is None else json.loads(existing["result"])
                return InvocationLease(
                    key,
                    acquired=False,
                    result=result,
                    effect_ref=existing["effect_ref"],
                )
            raise InvocationInFlightError("相同工具调用正在执行，请稍后重试")

        lease = await self._write(operation)
        if lease is None:
            raise InvocationOutcomeUnknownError()
        return lease

    async def complete_invocation(
        self,
        *,
        key: str,
        worker_id: str,
        result: dict[str, Any],
        effect_ref: str,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            changed = connection.execute(
                """
                UPDATE tool_invocations
                SET status = 'succeeded', result = ?, effect_ref = ?, lease_owner = NULL,
                    lease_until = NULL, completed_at = ?, updated_at = ?
                WHERE idempotency_key = ? AND status = 'in_flight' AND lease_owner = ?
                """,
                (canonical_json(result), effect_ref, _iso(), _iso(), key, worker_id),
            ).rowcount
            if changed != 1:
                raise InvocationInFlightError("工具调用租约已被其他 worker 接管")

        await self._write(operation)

    async def has_invocation(self, *, key: str) -> bool:
        return await self._read(
            lambda connection: (
                connection.execute(
                    "SELECT 1 FROM tool_invocations WHERE idempotency_key = ?", (key,)
                ).fetchone()
                is not None
            )
        )

    async def fail_invocation(self, *, key: str, worker_id: str, error: str) -> None:
        safe_error = str(redact_persisted_tool_value(error)).strip()[:160]

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                UPDATE tool_invocations
                SET status = 'failed', result = ?, lease_owner = NULL, lease_until = NULL,
                    updated_at = ?
                WHERE idempotency_key = ? AND status = 'in_flight' AND lease_owner = ?
                """,
                (
                    canonical_json({"error": safe_error or "tool_handler_failed"}),
                    _iso(),
                    key,
                    worker_id,
                ),
            )

        await self._write(operation)

    async def mark_invocation_outcome_unknown(self, *, key: str, worker_id: str) -> None:
        """Terminalize a dispatched call whose remote effect cannot be proven either way."""

        now = _iso()

        def operation(connection: sqlite3.Connection) -> None:
            changed = connection.execute(
                """
                UPDATE tool_invocations
                SET status = 'outcome_unknown', result = ?, effect_ref = NULL,
                    lease_owner = NULL, lease_until = NULL, completed_at = ?, updated_at = ?
                WHERE idempotency_key = ? AND status = 'in_flight' AND lease_owner = ?
                """,
                (
                    canonical_json(
                        {
                            "outcome": "unknown",
                            "reason": "remote_result_unavailable",
                        }
                    ),
                    now,
                    now,
                    key,
                    worker_id,
                ),
            ).rowcount
            if changed != 1:
                raise InvocationInFlightError("工具调用租约已被其他 worker 接管")

        await self._write(operation)

    async def claim_due_schedules(self, *, now_iso: str, limit: int = 50) -> list[UUID]:
        owner = f"scheduler:{os.getpid()}"
        lease_until = _iso(datetime.fromisoformat(now_iso) + timedelta(seconds=30))

        def operation(connection: sqlite3.Connection) -> list[UUID]:
            rows = connection.execute(
                """
                SELECT id FROM cowork_schedules
                WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
                  AND (dispatch_lease_until IS NULL OR dispatch_lease_until < ?)
                ORDER BY next_run_at, id LIMIT ?
                """,
                (now_iso, now_iso, limit),
            ).fetchall()
            ids = [UUID(row["id"]) for row in rows]
            for schedule_id in ids:
                connection.execute(
                    """
                    UPDATE cowork_schedules
                    SET dispatch_lease_owner = ?, dispatch_lease_until = ?, updated_at = ?
                    WHERE id = ? AND (dispatch_lease_until IS NULL OR dispatch_lease_until < ?)
                    """,
                    (owner, lease_until, now_iso, str(schedule_id), now_iso),
                )
            return ids

        return await self._write(operation)

    async def create_session_root(
        self,
        *,
        conversation_id: UUID,
        requested_path: str,
        access_mode: str,
        label: str | None = None,
    ) -> Any:
        if access_mode not in {"read_only", "read_write"}:
            raise ValueError("access_mode 只能是 read_only 或 read_write")
        canonical = await asyncio.to_thread(canonicalize_root, requested_path)
        display_label = (label or canonical.name or str(canonical)).strip()
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> Any:
            if (
                connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (str(conversation_id),)
                ).fetchone()
                is None
            ):
                raise ConversationNotFoundError(str(conversation_id))
            existing = connection.execute(
                "SELECT id FROM session_roots WHERE conversation_id = ? AND canonical_path = ?",
                (str(conversation_id), str(canonical)),
            ).fetchone()
            root_id = uuid7() if existing is None else UUID(existing["id"])
            connection.execute(
                """
                INSERT INTO session_roots(
                    id, conversation_id, requested_path, canonical_path, label,
                    access_mode, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(conversation_id, canonical_path) DO UPDATE SET
                    requested_path = excluded.requested_path,
                    label = excluded.label,
                    access_mode = excluded.access_mode,
                    enabled = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    str(root_id),
                    str(conversation_id),
                    requested_path,
                    str(canonical),
                    display_label,
                    access_mode,
                    timestamp,
                    timestamp,
                ),
            )
            allowed = ["filesystem.read"]
            if access_mode == "read_write":
                allowed.append("filesystem.write")
            else:
                connection.execute(
                    """
                    UPDATE capability_grants SET revoked_at = ?, updated_at = ?
                    WHERE conversation_id = ? AND session_root_id = ?
                      AND capability IN ('filesystem.write','office.word.edit','office.excel.edit')
                      AND revoked_at IS NULL
                    """,
                    (timestamp, timestamp, str(conversation_id), str(root_id)),
                )
            for capability in allowed:
                row = connection.execute(
                    """
                    SELECT id FROM capability_grants
                    WHERE conversation_id = ? AND session_root_id = ? AND capability = ?
                      AND revoked_at IS NULL
                    """,
                    (str(conversation_id), str(root_id), capability),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO capability_grants(
                            id, conversation_id, session_root_id, capability, grant_source,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'user', ?, ?)
                        """,
                        (
                            str(uuid7()),
                            str(conversation_id),
                            str(root_id),
                            capability,
                            timestamp,
                            timestamp,
                        ),
                    )
            row = connection.execute(
                "SELECT * FROM session_roots WHERE id = ?", (str(root_id),)
            ).fetchone()
            if row is None:  # pragma: no cover - 同事务刚写入
                raise CapabilityDeniedError("会话目录写入失败")
            return SessionRootRecord(
                id=UUID(row["id"]),
                conversation_id=UUID(row["conversation_id"]),
                requested_path=str(row["requested_path"]),
                canonical_path=str(row["canonical_path"]),
                label=str(row["label"]),
                access_mode=row["access_mode"],
                enabled=bool(row["enabled"]),
                created_at=cast(datetime, _datetime(row["created_at"])),
                updated_at=cast(datetime, _datetime(row["updated_at"])),
            )

        return await self._write(operation)

    async def list_session_roots(self, *, conversation_id: UUID) -> list[Any]:
        def operation(connection: sqlite3.Connection) -> list[Any]:
            if (
                connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (str(conversation_id),)
                ).fetchone()
                is None
            ):
                raise ConversationNotFoundError(str(conversation_id))
            rows = connection.execute(
                """
                SELECT * FROM session_roots
                WHERE conversation_id = ? AND enabled = 1
                ORDER BY CASE WHEN label = 'WorkPilot 默认文件夹' THEN 1 ELSE 0 END,
                         created_at, id
                """,
                (str(conversation_id),),
            ).fetchall()
            return [
                SessionRootRecord(
                    id=UUID(row["id"]),
                    conversation_id=UUID(row["conversation_id"]),
                    requested_path=str(row["requested_path"]),
                    canonical_path=str(row["canonical_path"]),
                    label=str(row["label"]),
                    access_mode=row["access_mode"],
                    enabled=bool(row["enabled"]),
                    created_at=cast(datetime, _datetime(row["created_at"])),
                    updated_at=cast(datetime, _datetime(row["updated_at"])),
                )
                for row in rows
            ]

        return await self._read(operation)

    async def revoke_session_root(self, *, conversation_id: UUID, root_id: UUID) -> bool:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> bool:
            changed = connection.execute(
                """UPDATE session_roots SET enabled = 0, updated_at = ?
                   WHERE id = ? AND conversation_id = ? AND enabled = 1""",
                (timestamp, str(root_id), str(conversation_id)),
            ).rowcount
            if changed:
                connection.execute(
                    """UPDATE capability_grants SET revoked_at = ?, updated_at = ?
                       WHERE conversation_id = ? AND session_root_id = ? AND revoked_at IS NULL""",
                    (timestamp, timestamp, str(conversation_id), str(root_id)),
                )
            return changed == 1

        return await self._write(operation)

    async def grant_capability(
        self,
        *,
        conversation_id: UUID,
        capability: str,
        session_root_id: UUID | None = None,
        resource_scope: str | None = None,
        grant_source: Literal["user", "policy"] = "user",
        expires_in_s: int | None = None,
    ) -> Any:
        if capability not in ALL_CAPABILITIES:
            raise ValueError("未知 capability")
        if capability in PATH_CAPABILITIES and session_root_id is None:
            raise ValueError("文件 capability 必须绑定会话目录")
        if capability in GLOBAL_CAPABILITIES and session_root_id is not None:
            raise ValueError("网络/Shell/外部操作 capability 不能继承目录授权")
        if capability in SCOPED_CAPABILITIES:
            if resource_scope is None:
                raise ValueError(f"{capability} 必须绑定资源 scope")
            resource_scope = normalize_network_scope(resource_scope)
        elif resource_scope is not None:
            raise ValueError(f"{capability} 不能绑定资源 scope")
        now = _now()
        expires_at = None if expires_in_s is None else now + timedelta(seconds=expires_in_s)

        def operation(connection: sqlite3.Connection) -> Any:
            if (
                connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (str(conversation_id),)
                ).fetchone()
                is None
            ):
                raise ConversationNotFoundError(str(conversation_id))
            if session_root_id is not None:
                root = connection.execute(
                    """SELECT access_mode FROM session_roots
                       WHERE id = ? AND conversation_id = ? AND enabled = 1""",
                    (str(session_root_id), str(conversation_id)),
                ).fetchone()
                if root is None:
                    raise SessionRootNotFoundError(str(session_root_id))
                if capability != "filesystem.read" and root["access_mode"] != "read_write":
                    raise CapabilityDeniedError("只读目录不能授予写入或 Office 编辑能力")
            timestamp = _iso(now)
            connection.execute(
                """UPDATE capability_grants SET revoked_at = ?, updated_at = ?
                   WHERE conversation_id = ? AND IFNULL(session_root_id, '') = IFNULL(?, '')
                     AND capability = ? AND IFNULL(resource_scope, '') = IFNULL(?, '')
                     AND revoked_at IS NULL
                     AND expires_at IS NOT NULL AND expires_at <= ?""",
                (
                    timestamp,
                    timestamp,
                    str(conversation_id),
                    None if session_root_id is None else str(session_root_id),
                    capability,
                    resource_scope,
                    timestamp,
                ),
            )
            row = connection.execute(
                """SELECT * FROM capability_grants
                   WHERE conversation_id = ? AND IFNULL(session_root_id, '') = IFNULL(?, '')
                     AND capability = ? AND IFNULL(resource_scope, '') = IFNULL(?, '')
                     AND revoked_at IS NULL""",
                (
                    str(conversation_id),
                    None if session_root_id is None else str(session_root_id),
                    capability,
                    resource_scope,
                ),
            ).fetchone()
            if row is None:
                grant_id = uuid7()
                connection.execute(
                    """INSERT INTO capability_grants(
                           id, conversation_id, session_root_id, capability, resource_scope,
                           grant_source, expires_at, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(grant_id),
                        str(conversation_id),
                        None if session_root_id is None else str(session_root_id),
                        capability,
                        resource_scope,
                        grant_source,
                        None if expires_at is None else _iso(expires_at),
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM capability_grants WHERE id = ?", (str(grant_id),)
                ).fetchone()
            else:
                connection.execute(
                    """UPDATE capability_grants SET expires_at = ?, grant_source = ?, updated_at = ?
                       WHERE id = ?""",
                    (
                        None if expires_at is None else _iso(expires_at),
                        grant_source,
                        timestamp,
                        row["id"],
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM capability_grants WHERE id = ?", (row["id"],)
                ).fetchone()
            assert row is not None
            return self._grant_record(row, CapabilityGrantRecord)

        return await self._write(operation)

    async def list_capability_grants(self, *, conversation_id: UUID) -> list[Any]:
        now = _iso()

        def operation(connection: sqlite3.Connection) -> list[Any]:
            if (
                connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (str(conversation_id),)
                ).fetchone()
                is None
            ):
                raise ConversationNotFoundError(str(conversation_id))
            rows = connection.execute(
                """SELECT * FROM capability_grants
                   WHERE conversation_id = ? AND revoked_at IS NULL
                     AND (expires_at IS NULL OR expires_at > ?)
                   ORDER BY created_at, id""",
                (str(conversation_id), now),
            ).fetchall()
            return [self._grant_record(row, CapabilityGrantRecord) for row in rows]

        return await self._read(operation)

    # ---- 消息面 -------------------------------------------------------------
    async def upsert_inbox_binding(
        self,
        *,
        name: str,
        platform: str | None,
        chat_id: str | None,
        connector_account_id: UUID | None,
        enabled: bool,
    ) -> InboxBindingRecord:
        def operation(connection: sqlite3.Connection) -> InboxBindingRecord:
            connection.execute(
                """INSERT INTO cowork_inbox_bindings(
                       id, name, platform, chat_id, connector_account_id, enabled, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       platform = excluded.platform,
                       chat_id = excluded.chat_id,
                       connector_account_id = excluded.connector_account_id,
                       enabled = excluded.enabled""",
                (
                    str(uuid7()),
                    name,
                    platform,
                    chat_id,
                    None if connector_account_id is None else str(connector_account_id),
                    int(enabled),
                    _iso(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM cowork_inbox_bindings WHERE name = ?", (name,)
            ).fetchone()
            return self._inbox_binding_record(row)

        return await self._write(operation)

    async def get_inbox_binding(self, *, name: str) -> InboxBindingRecord | None:
        def operation(connection: sqlite3.Connection) -> InboxBindingRecord | None:
            row = connection.execute(
                "SELECT * FROM cowork_inbox_bindings WHERE name = ?", (name,)
            ).fetchone()
            return None if row is None else self._inbox_binding_record(row)

        return await self._read(operation)

    async def list_inbox_bindings(self) -> list[InboxBindingRecord]:
        return await self._read(
            lambda connection: [
                self._inbox_binding_record(row)
                for row in connection.execute(
                    "SELECT * FROM cowork_inbox_bindings ORDER BY name"
                ).fetchall()
            ]
        )

    async def delete_inbox_binding(self, *, name: str) -> bool:
        return await self._write(
            lambda connection: (
                connection.execute(
                    "DELETE FROM cowork_inbox_bindings WHERE name = ?", (name,)
                ).rowcount
                == 1
            )
        )

    async def set_conversation_inbox(
        self, *, conversation_id: UUID, inbox_name: str | None
    ) -> bool:
        return await self._write(
            lambda connection: (
                connection.execute(
                    "UPDATE conversations SET inbox_name = ?, updated_at = ? WHERE id = ?",
                    (inbox_name, _iso(), str(conversation_id)),
                ).rowcount
                == 1
            )
        )

    async def get_conversation_inbox(self, *, conversation_id: UUID) -> str | None:
        return await self._read(
            lambda connection: (lambda row: None if row is None else row[0])(
                connection.execute(
                    "SELECT inbox_name FROM conversations WHERE id = ?",
                    (str(conversation_id),),
                ).fetchone()
            )
        )

    async def set_conversation_kb(self, *, conversation_id: UUID, kb_slug: str | None) -> bool:
        return await self._write(
            lambda connection: (
                connection.execute(
                    "UPDATE conversations SET kb_slug = ?, updated_at = ? WHERE id = ?",
                    (kb_slug, _iso(), str(conversation_id)),
                ).rowcount
                == 1
            )
        )

    async def get_conversation_kb(self, *, conversation_id: UUID) -> str | None:
        return await self._read(
            lambda connection: (lambda row: None if row is None else row[0])(
                connection.execute(
                    "SELECT kb_slug FROM conversations WHERE id = ?",
                    (str(conversation_id),),
                ).fetchone()
            )
        )

    async def set_inbox_delivery_ref(self, *, item_id: UUID, delivery_ref: str) -> None:
        await self._write(
            lambda connection: connection.execute(
                "UPDATE cowork_inbox_items SET delivery_ref = ? WHERE id = ?",
                (delivery_ref, str(item_id)),
            )
        )

    async def get_inbox_item_by_id(self, *, item_id: UUID) -> InboxRecord | None:
        def operation(connection: sqlite3.Connection) -> InboxRecord | None:
            row = connection.execute(
                "SELECT * FROM cowork_inbox_items WHERE id = ?", (str(item_id),)
            ).fetchone()
            return None if row is None else self._inbox_record(row, InboxRecord)

        return await self._read(operation)

    async def claim_messaging_event(
        self,
        *,
        event_key: str,
        platform: str,
        event_type: str,
        retention_days: int,
    ) -> bool:
        if (
            len(event_key) != 64
            or any(character not in "0123456789abcdef" for character in event_key)
            or not 1 <= len(platform) <= 32
            or not 1 <= len(event_type) <= 256
            or not 1 <= retention_days <= 365
        ):
            raise ValueError("消息事件 receipt 参数无效")
        now = _now()
        cutoff = _iso(now - timedelta(days=retention_days))

        def operation(connection: sqlite3.Connection) -> bool:
            connection.execute(
                "DELETE FROM cowork_messaging_event_receipts WHERE received_at < ?",
                (cutoff,),
            )
            changed = connection.execute(
                """INSERT INTO cowork_messaging_event_receipts(
                       event_key, platform, event_type, status, received_at
                   ) VALUES (?, ?, ?, 'claimed', ?) ON CONFLICT(event_key) DO NOTHING""",
                (event_key, platform, event_type, _iso(now)),
            ).rowcount
            return changed == 1

        return await self._write(operation)

    async def complete_messaging_event(self, *, event_key: str) -> bool:
        return await self._write(
            lambda connection: (
                connection.execute(
                    """UPDATE cowork_messaging_event_receipts
                       SET status = 'completed', completed_at = ?
                       WHERE event_key = ? AND status = 'claimed'""",
                    (_iso(), event_key),
                ).rowcount
                == 1
            )
        )

    async def create_channel_subscription(
        self,
        *,
        conversation_id: UUID,
        platform: str,
        chat_id: str,
        connector_account_id: UUID | None,
    ) -> ChannelSubscriptionRecord:
        def operation(connection: sqlite3.Connection) -> ChannelSubscriptionRecord:
            existing = connection.execute(
                """SELECT * FROM cowork_channel_subscriptions
                   WHERE conversation_id = ? AND platform = ? AND chat_id = ?
                     AND revoked_at IS NULL""",
                (str(conversation_id), platform, chat_id),
            ).fetchone()
            if existing is not None:
                return self._channel_subscription_record(existing)
            subscription_id = uuid7()
            connection.execute(
                """INSERT INTO cowork_channel_subscriptions(
                       id, conversation_id, platform, chat_id, connector_account_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(subscription_id),
                    str(conversation_id),
                    platform,
                    chat_id,
                    None if connector_account_id is None else str(connector_account_id),
                    _iso(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM cowork_channel_subscriptions WHERE id = ?",
                (str(subscription_id),),
            ).fetchone()
            return self._channel_subscription_record(row)

        return await self._write(operation)

    async def list_channel_subscriptions(
        self, *, conversation_id: UUID | None = None, channel: tuple[str, str] | None = None
    ) -> list[ChannelSubscriptionRecord]:
        def operation(connection: sqlite3.Connection) -> list[ChannelSubscriptionRecord]:
            clauses = ["revoked_at IS NULL"]
            parameters: list[Any] = []
            if conversation_id is not None:
                clauses.append("conversation_id = ?")
                parameters.append(str(conversation_id))
            if channel is not None:
                clauses.append("platform = ? AND chat_id = ?")
                parameters.extend(channel)
            rows = connection.execute(
                f"""SELECT * FROM cowork_channel_subscriptions
                    WHERE {" AND ".join(clauses)} ORDER BY created_at""",
                tuple(parameters),
            ).fetchall()
            return [self._channel_subscription_record(row) for row in rows]

        return await self._read(operation)

    async def revoke_channel_subscription(
        self, *, conversation_id: UUID, subscription_id: UUID
    ) -> bool:
        return await self._write(
            lambda connection: (
                connection.execute(
                    """UPDATE cowork_channel_subscriptions SET revoked_at = ?
                       WHERE id = ? AND conversation_id = ? AND revoked_at IS NULL""",
                    (_iso(), str(subscription_id), str(conversation_id)),
                ).rowcount
                == 1
            )
        )

    async def upsert_thread_session(
        self,
        *,
        target: str,
        conversation_id: UUID,
        platform: str,
        chat_id: str,
        thread_id: str,
    ) -> ThreadSessionRecord:
        def operation(connection: sqlite3.Connection) -> ThreadSessionRecord:
            connection.execute(
                """INSERT INTO cowork_thread_sessions(
                       target, conversation_id, platform, chat_id, thread_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(target) DO NOTHING""",
                (
                    target,
                    str(conversation_id),
                    platform,
                    chat_id,
                    thread_id,
                    _iso(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM cowork_thread_sessions WHERE target = ?", (target,)
            ).fetchone()
            return self._thread_session_record(row)

        return await self._write(operation)

    async def get_thread_session(self, *, target: str) -> ThreadSessionRecord | None:
        def operation(connection: sqlite3.Connection) -> ThreadSessionRecord | None:
            row = connection.execute(
                "SELECT * FROM cowork_thread_sessions WHERE target = ?", (target,)
            ).fetchone()
            return None if row is None else self._thread_session_record(row)

        return await self._read(operation)

    async def list_thread_sessions(self, *, conversation_id: UUID) -> list[ThreadSessionRecord]:
        return await self._read(
            lambda connection: [
                self._thread_session_record(row)
                for row in connection.execute(
                    """SELECT * FROM cowork_thread_sessions WHERE conversation_id = ?
                       ORDER BY created_at""",
                    (str(conversation_id),),
                ).fetchall()
            ]
        )

    async def record_unrouted(
        self,
        *,
        kind: str,
        platform: str | None,
        chat_id: str | None,
        summary: str,
        payload: dict[str, Any],
        keep: int,
    ) -> UnroutedRecord:
        def operation(connection: sqlite3.Connection) -> UnroutedRecord:
            record_id = uuid7()
            connection.execute(
                """INSERT INTO cowork_unrouted(
                       id, kind, platform, chat_id, summary, payload, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(record_id),
                    kind,
                    platform,
                    chat_id,
                    summary,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    _iso(),
                ),
            )
            # 死信是可见性设施，不是队列：留最近的一批就够，无上限地攒下去只会让
            # 本地库慢慢变大而没人看。
            connection.execute(
                """DELETE FROM cowork_unrouted WHERE id NOT IN (
                       SELECT id FROM cowork_unrouted ORDER BY created_at DESC LIMIT ?
                   )""",
                (keep,),
            )
            row = connection.execute(
                "SELECT * FROM cowork_unrouted WHERE id = ?", (str(record_id),)
            ).fetchone()
            return self._unrouted_record(row)

        return await self._write(operation)

    async def list_unrouted(self, *, limit: int) -> list[UnroutedRecord]:
        return await self._read(
            lambda connection: [
                self._unrouted_record(row)
                for row in connection.execute(
                    "SELECT * FROM cowork_unrouted ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]
        )

    # --- 阅读批注 ----------------------------------------------------------

    def _annotation_record(self, row: sqlite3.Row) -> ReadingAnnotationRecord:
        return ReadingAnnotationRecord(
            id=UUID(str(row["id"])),
            material_id=str(row["material_id"]),
            path=str(row["path"]),
            locator=int(row["locator"]),
            quote=str(row["quote"]),
            note=str(row["note"]),
            color=cast(AnnotationColor, str(row["color"])),
            locations=tuple(json.loads(str(row["locations"]))),
            conversation_id=(UUID(str(row["conversation_id"])) if row["conversation_id"] else None),
            run_id=UUID(str(row["run_id"])) if row["run_id"] else None,
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

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
    ) -> ReadingAnnotationRecord:
        """写入一条批注；同一份材料上超过上限即拒绝。

        上限在**写事务里**判定而不是先查后写：一轮里模型可以并行调多次，
        先查后写会让"最多 N 条"变成"大约 N 条"。
        """

        def operation(connection: sqlite3.Connection) -> ReadingAnnotationRecord:
            existing = connection.execute(
                """SELECT COUNT(*) FROM cowork_reading_annotations
                   WHERE material_id = ? AND deleted_at IS NULL""",
                (material_id,),
            ).fetchone()[0]
            if int(existing) >= max_per_material:
                raise ValueError(
                    f"这份文档上的批注已达上限 {max_per_material} 条，"
                    "先让用户在阅读器里删掉一些再继续标注。"
                )
            annotation_id = str(uuid7())
            connection.execute(
                """INSERT INTO cowork_reading_annotations(
                       id, material_id, path, locator, quote, note, color,
                       locations, conversation_id, run_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    annotation_id,
                    material_id,
                    path,
                    locator,
                    quote,
                    note,
                    color,
                    json.dumps(list(locations), ensure_ascii=False),
                    str(conversation_id) if conversation_id else None,
                    str(run_id) if run_id else None,
                    _iso(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM cowork_reading_annotations WHERE id = ?",
                (annotation_id,),
            ).fetchone()
            return self._annotation_record(row)

        return await self._write(operation)

    async def list_reading_annotations(self, *, material_id: str) -> list[ReadingAnnotationRecord]:
        return await self._read(
            lambda connection: [
                self._annotation_record(row)
                for row in connection.execute(
                    """SELECT * FROM cowork_reading_annotations
                       WHERE material_id = ? AND deleted_at IS NULL
                       ORDER BY locator, created_at""",
                    (material_id,),
                ).fetchall()
            ]
        )

    async def count_stale_reading_annotations(self, *, path: str, material_id: str) -> int:
        """同一路径上属于**别的**内容版本的批注条数。

        它们不显示——几何可能已经指向别的文字。但也不能静默消失：
        用户改了一版 PDF 之后看到批注全没了却查不到为什么，是最糟的那种失败。
        """

        return await self._read(
            lambda connection: int(
                connection.execute(
                    """SELECT COUNT(*) FROM cowork_reading_annotations
                       WHERE path = ? AND material_id <> ? AND deleted_at IS NULL""",
                    (path, material_id),
                ).fetchone()[0]
            )
        )

    async def delete_reading_annotation(self, *, annotation_id: UUID) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            return (
                connection.execute(
                    """UPDATE cowork_reading_annotations SET deleted_at = ?
                       WHERE id = ? AND deleted_at IS NULL""",
                    (_iso(), str(annotation_id)),
                ).rowcount
                == 1
            )

        return await self._write(operation)

    async def set_workspace_trust(
        self,
        *,
        canonical_path: str,
        trusted: bool,
        policy_sha256: str | None,
    ) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            if trusted:
                if policy_sha256 is None:
                    raise ValueError("信任 workspace 必须绑定 policy_sha256")
                connection.execute(
                    """INSERT INTO cowork_workspace_trust(
                           canonical_path, policy_sha256, trusted_at, revoked_at
                       ) VALUES (?, ?, ?, NULL)
                       ON CONFLICT(canonical_path) DO UPDATE
                       SET policy_sha256 = excluded.policy_sha256,
                           trusted_at = excluded.trusted_at,
                           revoked_at = NULL""",
                    (canonical_path, policy_sha256, _iso()),
                )
                return True
            return (
                connection.execute(
                    """UPDATE cowork_workspace_trust SET revoked_at = ?
                       WHERE canonical_path = ? AND revoked_at IS NULL""",
                    (_iso(), canonical_path),
                ).rowcount
                == 1
            )

        return await self._write(operation)

    async def is_workspace_trusted(
        self,
        *,
        canonical_path: str,
        policy_sha256: str,
    ) -> bool:
        return await self._read(
            lambda connection: (
                connection.execute(
                    """SELECT 1 FROM cowork_workspace_trust
                   WHERE canonical_path = ? AND policy_sha256 = ? AND revoked_at IS NULL""",
                    (canonical_path, policy_sha256),
                ).fetchone()
                is not None
            )
        )

    async def list_workspace_trust(self) -> list[str]:
        return await self._read(
            lambda connection: [
                str(row[0])
                for row in connection.execute(
                    """SELECT canonical_path FROM cowork_workspace_trust
                       WHERE revoked_at IS NULL ORDER BY canonical_path"""
                ).fetchall()
            ]
        )

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
    ) -> ApprovalRuleRecord:
        def operation(connection: sqlite3.Connection) -> ApprovalRuleRecord:
            existing = connection.execute(
                """SELECT * FROM cowork_approval_rules
                   WHERE conversation_id = ? AND tool = ? AND match_kind = ?
                     AND IFNULL(target, '') = IFNULL(?, '')
                     AND IFNULL(schedule_id, '') = IFNULL(?, '')
                     AND revoked_at IS NULL""",
                (
                    str(conversation_id),
                    tool,
                    match_kind,
                    target,
                    None if schedule_id is None else str(schedule_id),
                ),
            ).fetchone()
            if existing is not None:
                return self._approval_rule_record(existing)
            rule_id = uuid7()
            connection.execute(
                """INSERT INTO cowork_approval_rules(
                       id, conversation_id, scope, schedule_id, tool, match_kind,
                       target, created_by, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(rule_id),
                    str(conversation_id),
                    scope,
                    None if schedule_id is None else str(schedule_id),
                    tool,
                    match_kind,
                    target,
                    created_by,
                    _iso(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM cowork_approval_rules WHERE id = ?", (str(rule_id),)
            ).fetchone()
            return self._approval_rule_record(row)

        return await self._write(operation)

    async def list_approval_rules(
        self, *, conversation_id: UUID, include_revoked: bool = False
    ) -> list[ApprovalRuleRecord]:
        def operation(connection: sqlite3.Connection) -> list[ApprovalRuleRecord]:
            rows = connection.execute(
                """SELECT * FROM cowork_approval_rules
                   WHERE conversation_id = ? AND (? OR revoked_at IS NULL)
                   ORDER BY created_at DESC""",
                (str(conversation_id), 1 if include_revoked else 0),
            ).fetchall()
            return [self._approval_rule_record(row) for row in rows]

        return await self._read(operation)

    async def revoke_approval_rule(self, *, conversation_id: UUID, rule_id: UUID) -> bool:
        return await self._write(
            lambda connection: (
                connection.execute(
                    """UPDATE cowork_approval_rules SET revoked_at = ?
                       WHERE id = ? AND conversation_id = ? AND revoked_at IS NULL""",
                    (_iso(), str(rule_id), str(conversation_id)),
                ).rowcount
                == 1
            )
        )

    async def revoke_capability_grant(self, *, conversation_id: UUID, grant_id: UUID) -> bool:
        timestamp = _iso()
        return await self._write(
            lambda connection: (
                connection.execute(
                    """UPDATE capability_grants SET revoked_at = ?, updated_at = ?
                   WHERE id = ? AND conversation_id = ? AND revoked_at IS NULL""",
                    (timestamp, timestamp, str(grant_id), str(conversation_id)),
                ).rowcount
                == 1
            )
        )

    async def authorize_capability(self, *, conversation_id: UUID, capability: str) -> Any:
        if capability not in GLOBAL_CAPABILITIES:
            raise ValueError("路径 capability 必须通过 authorize_path 校验")
        if capability in SCOPED_CAPABILITIES:
            raise ValueError("带资源 scope 的 capability 必须通过 authorize_scoped_capability 校验")
        now = _iso()
        candidates = (capability, *LEGACY_CAPABILITY_FALLBACKS.get(capability, ()))
        placeholders = ", ".join("?" for _ in candidates)

        def operation(connection: sqlite3.Connection) -> Any:
            row = connection.execute(
                f"""SELECT * FROM capability_grants
                   WHERE conversation_id = ? AND capability IN ({placeholders})
                     AND session_root_id IS NULL AND resource_scope IS NULL
                     AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?)
                   ORDER BY created_at DESC LIMIT 1""",
                (str(conversation_id), *candidates, now),
            ).fetchone()
            if row is None:
                raise CapabilityDeniedError(f"尚未授予 {capability} 权限")
            return self._grant_record(row, CapabilityGrantRecord)

        return await self._read(operation)

    async def authorize_scoped_capability(
        self, *, conversation_id: UUID, capability: str, target: str
    ) -> Any:
        if capability not in SCOPED_CAPABILITIES:
            raise ValueError("该 capability 不支持资源 scope")
        now = _iso()

        def operation(connection: sqlite3.Connection) -> Any:
            rows = connection.execute(
                """SELECT * FROM capability_grants
                   WHERE conversation_id = ? AND capability = ? AND session_root_id IS NULL
                     AND resource_scope IS NOT NULL AND revoked_at IS NULL
                     AND (expires_at IS NULL OR expires_at > ?)
                   ORDER BY created_at DESC""",
                (str(conversation_id), capability, now),
            ).fetchall()
            for row in rows:
                if network_scope_allows(str(row["resource_scope"]), target):
                    return self._grant_record(row, CapabilityGrantRecord)

            # 迁移期兼容既有无 scope 的 network.read；新 API 无法再创建它。
            for legacy in LEGACY_CAPABILITY_FALLBACKS.get(capability, ()):
                row = connection.execute(
                    """SELECT * FROM capability_grants
                       WHERE conversation_id = ? AND capability = ?
                         AND session_root_id IS NULL AND resource_scope IS NULL
                         AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?)
                       ORDER BY created_at DESC LIMIT 1""",
                    (str(conversation_id), legacy, now),
                ).fetchone()
                if row is not None:
                    return self._grant_record(row, CapabilityGrantRecord)
            raise CapabilityDeniedError(f"目标 {target} 尚未获得 {capability} 权限")

        return await self._read(operation)

    async def authorize_path(
        self, *, conversation_id: UUID, target_path: Path, capability: str
    ) -> Any:
        if capability not in PATH_CAPABILITIES:
            raise ValueError("非路径 capability 必须通过 authorize_capability 校验")
        now = _iso()

        def operation(connection: sqlite3.Connection) -> Any:
            rows = connection.execute(
                """SELECT roots.id, roots.canonical_path, roots.access_mode,
                          grants.id AS grant_id
                   FROM session_roots AS roots
                   JOIN capability_grants AS grants ON grants.session_root_id = roots.id
                   WHERE roots.conversation_id = ? AND roots.enabled = 1
                     AND grants.capability = ? AND grants.revoked_at IS NULL
                     AND (grants.expires_at IS NULL OR grants.expires_at > ?)
                   ORDER BY length(roots.canonical_path) DESC""",
                (str(conversation_id), capability, now),
            ).fetchall()
            for row in rows:
                root = Path(row["canonical_path"])
                try:
                    resolved = resolve_target_within_root(root, target_path)
                except CapabilityDeniedError:
                    continue
                if capability != "filesystem.read" and row["access_mode"] != "read_write":
                    continue
                if capability == "office.word.edit" and resolved.suffix.lower() != ".docx":
                    continue
                if capability == "office.excel.edit" and resolved.suffix.lower() != ".xlsx":
                    continue
                return PathAuthorization(
                    conversation_id=conversation_id,
                    root_id=UUID(row["id"]),
                    root_path=root,
                    target_path=resolved,
                    access_mode=row["access_mode"],
                    capability=capability,  # type: ignore[arg-type]
                    grant_id=UUID(row["grant_id"]),
                )
            raise CapabilityDeniedError(f"目标路径未获得 {capability} 权限")

        return await self._read(operation)

    async def register_artifact(
        self,
        *,
        conversation_id: UUID,
        kind: str,
        title: str,
        uri: str,
        run_id: UUID | None = None,
        session_root_id: UUID | None = None,
        mime_type: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Any:
        if kind not in {"file", "report", "diff", "table"}:
            raise ValueError("未知 artifact kind")
        if not title.strip() or not uri.strip():
            raise ValueError("artifact 标题与 URI 不能为空")

        def operation(connection: sqlite3.Connection) -> Any:
            if (
                connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (str(conversation_id),)
                ).fetchone()
                is None
            ):
                raise ConversationNotFoundError(str(conversation_id))
            if (
                run_id is not None
                and connection.execute(
                    "SELECT 1 FROM agent_runs WHERE id = ? AND conversation_id = ?",
                    (str(run_id), str(conversation_id)),
                ).fetchone()
                is None
            ):
                raise ArtifactRegistrationError("artifact 的 run 不属于当前 Cowork 会话")
            stored_uri = uri.strip()
            if session_root_id is not None:
                root = connection.execute(
                    """SELECT canonical_path FROM session_roots
                       WHERE id = ? AND conversation_id = ? AND enabled = 1""",
                    (str(session_root_id), str(conversation_id)),
                ).fetchone()
                if root is None:
                    raise ArtifactRegistrationError("artifact 绑定的会话目录不存在或已撤销")
                try:
                    stored_uri = str(
                        resolve_target_within_root(Path(root["canonical_path"]), Path(stored_uri))
                    )
                except CapabilityDeniedError as error:
                    raise ArtifactRegistrationError("artifact 路径不在绑定的会话目录内") from error
            artifact_id = uuid7()
            timestamp = _iso()
            connection.execute(
                """INSERT INTO artifacts(
                       id, conversation_id, run_id, session_root_id, kind, title, uri,
                       mime_type, meta, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(artifact_id),
                    str(conversation_id),
                    None if run_id is None else str(run_id),
                    None if session_root_id is None else str(session_root_id),
                    kind,
                    title.strip(),
                    stored_uri,
                    mime_type,
                    canonical_json(meta or {}),
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id = ?", (str(artifact_id),)
            ).fetchone()
            assert row is not None
            return self._artifact_record(row, ArtifactRecord)

        return await self._write(operation)

    async def list_artifacts(self, *, conversation_id: UUID) -> list[Any]:
        def operation(connection: sqlite3.Connection) -> list[Any]:
            if (
                connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (str(conversation_id),)
                ).fetchone()
                is None
            ):
                raise ConversationNotFoundError(str(conversation_id))
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE conversation_id = ? ORDER BY created_at DESC, id DESC",
                (str(conversation_id),),
            ).fetchall()
            # Artifact 记录是每次文件变更的审计快照，同一路径在一次任务里反复生成时会有
            # 多个 id；右栏回答的却是“现在有哪些交付文件”，不能把版本数冒充文件数。
            # 保留最新一条用于预览/差异，旧记录仍留在库里并可由 run 事件按 id 追溯。
            latest_rows: list[sqlite3.Row] = []
            seen_uris: set[str] = set()
            for row in rows:
                uri = str(row["uri"])
                if uri in seen_uris:
                    continue
                seen_uris.add(uri)
                latest_rows.append(row)
            return [self._artifact_record(row, ArtifactRecord) for row in latest_rows]

        return await self._read(operation)

    async def list_run_artifacts(self, *, run_id: UUID) -> list[Any]:
        return await self._read(
            lambda connection: [
                self._artifact_record(row, ArtifactRecord)
                for row in connection.execute(
                    "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at, id",
                    (str(run_id),),
                ).fetchall()
            ]
        )

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
    ) -> Any:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> Any:
            connection.execute(
                """INSERT INTO cowork_attachments(
                       id, conversation_id, kind, filename, media_type, storage_path,
                       size_bytes, sha256, extracted_text, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(attachment_id),
                    str(conversation_id),
                    kind,
                    filename,
                    media_type,
                    storage_path,
                    size_bytes,
                    sha256,
                    extracted_text,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM cowork_attachments WHERE id = ?", (str(attachment_id),)
            ).fetchone()
            assert row is not None
            return self._attachment_record(row, CoworkAttachmentRecord)

        return await self._write(operation)

    async def bind_attachments(
        self,
        *,
        conversation_id: UUID,
        attachment_ids: Sequence[UUID],
        message_id: UUID,
        run_id: UUID,
    ) -> list[Any]:
        def operation(connection: sqlite3.Connection) -> list[Any]:
            rows: list[sqlite3.Row] = []
            for attachment_id in attachment_ids:
                changed = connection.execute(
                    """UPDATE cowork_attachments SET message_id = ?, run_id = ?, updated_at = ?
                       WHERE id = ? AND conversation_id = ?
                         AND message_id IS NULL AND run_id IS NULL""",
                    (
                        str(message_id),
                        str(run_id),
                        _iso(),
                        str(attachment_id),
                        str(conversation_id),
                    ),
                ).rowcount
                if changed != 1:
                    raise CoworkAttachmentError("附件不存在、已被使用，或不属于当前会话")
                row = connection.execute(
                    "SELECT * FROM cowork_attachments WHERE id = ?", (str(attachment_id),)
                ).fetchone()
                assert row is not None
                rows.append(row)
            return [self._attachment_record(row, CoworkAttachmentRecord) for row in rows]

        return await self._write(operation)

    async def list_run_attachments(self, *, run_id: UUID) -> list[Any]:
        return await self._read(
            lambda connection: [
                self._attachment_record(row, CoworkAttachmentRecord)
                for row in connection.execute(
                    """SELECT * FROM cowork_attachments WHERE run_id = ?
                       ORDER BY created_at, id""",
                    (str(run_id),),
                ).fetchall()
            ]
        )

    async def resolve_artifact_file(self, *, artifact_id: UUID) -> tuple[Any, Path] | None:
        def operation(connection: sqlite3.Connection) -> tuple[Any, Path] | None:
            row = connection.execute(
                """SELECT artifacts.*, roots.canonical_path AS root_path
                   FROM artifacts JOIN session_roots AS roots ON roots.id = artifacts.session_root_id
                   WHERE artifacts.id = ? AND roots.enabled = 1""",
                (str(artifact_id),),
            ).fetchone()
            if row is None:
                return None
            artifact = self._artifact_record(row, ArtifactRecord)
            try:
                path = resolve_target_within_root(Path(row["root_path"]), Path(artifact.uri))
            except CapabilityDeniedError as error:
                raise ArtifactRegistrationError("artifact 文件已离开授权目录") from error
            if path.is_symlink() or not path.is_file():
                raise ArtifactRegistrationError("artifact 文件不存在或不是普通文件")
            return artifact, path

        return await self._read(operation)

    @staticmethod
    def _queued_message_record(
        row: sqlite3.Row,
        *,
        status: QueuedMessageStatus | None = None,
        consumed_at: datetime | None = None,
    ) -> SteeringRecord:
        return SteeringRecord(
            id=UUID(str(row["id"])),
            run_id=UUID(str(row["run_id"])),
            conversation_id=UUID(str(row["conversation_id"])),
            content=str(row["content"]),
            source=cast("SteeringSource", str(row["source"])),
            status=status or cast("QueuedMessageStatus", str(row["status"])),
            created_at=cast(datetime, _datetime(row["created_at"])),
            consumed_at=(
                consumed_at if consumed_at is not None else _datetime(row["consumed_at"])
            ),
            requested_delivery=cast(
                "QueuedMessageDelivery", str(row["requested_delivery"])
            ),
            delivery=cast("QueuedMessageDelivery", str(row["delivery"])),
            cancelled_at=_datetime(row["cancelled_at"]),
        )

    def _append_queue_record_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        phase: Literal["enqueued", "consumed", "cancelled"],
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "message_id": str(row["id"]),
            "requested_delivery": str(row["requested_delivery"]),
            "delivery": str(row["delivery"]),
            "source": str(row["source"]),
        }
        if extra is not None:
            payload.update(extra)
        self._append_session_record_transaction(
            connection,
            run_id=UUID(str(row["run_id"])),
            kind="queue_event",
            operation_id=f"queue:{row['id']}",
            phase=phase,
            payload=payload,
        )

    async def enqueue_steering(
        self,
        *,
        run_id: UUID,
        conversation_id: UUID,
        content: str,
        source: SteeringSource = "unknown",
        source_wake_id: UUID | None = None,
    ) -> SteeringRecord:
        return await self.enqueue_queued_message(
            run_id=run_id,
            conversation_id=conversation_id,
            content=content,
            source=source,
            delivery="steer",
            source_wake_id=source_wake_id,
        )

    async def enqueue_queued_message(
        self,
        *,
        run_id: UUID,
        conversation_id: UUID,
        content: str,
        source: SteeringSource,
        delivery: QueuedMessageDelivery,
        source_wake_id: UUID | None = None,
    ) -> SteeringRecord:
        normalized = content.strip()
        if not 1 <= len(normalized) <= 4000:
            raise ValueError("queued message 内容长度必须位于 1 到 4000")
        if source not in {"local_owner", "external_inbound", "runtime", "unknown"}:
            raise ValueError("queued message source 无效")
        if delivery not in {"steer", "follow_up", "next_run"}:
            raise ValueError("queued message delivery 无效")
        item_id = uuid7()
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> SteeringRecord:
            if source_wake_id is not None:
                existing = connection.execute(
                    "SELECT * FROM cowork_steering_messages WHERE source_wake_id = ?",
                    (str(source_wake_id),),
                ).fetchone()
                if existing is not None:
                    self._append_queue_record_transaction(
                        connection,
                        row=existing,
                        phase="enqueued",
                    )
                    return self._queued_message_record(existing)
            run = connection.execute(
                "SELECT conversation_id, status FROM agent_runs WHERE id = ?",
                (str(run_id),),
            ).fetchone()
            if run is None:
                raise RunNotFoundError(str(run_id))
            if str(run["conversation_id"]) != str(conversation_id):
                raise ValueError("queued message 的 run 与 conversation 不匹配")
            terminal = str(run["status"]) in TERMINAL_RUN_STATUSES
            effective_delivery = delivery
            if delivery == "steer" and terminal:
                raise ValueError("终态 run 不能接收 steering")
            if delivery in {"steer", "follow_up"}:
                queue_state = connection.execute(
                    "SELECT follow_up_open FROM cowork_run_queue_state WHERE run_id = ?",
                    (str(run_id),),
                ).fetchone()
                if (delivery == "follow_up" and terminal) or (
                    queue_state is not None and not bool(queue_state[0])
                ):
                    # The terminal boundary already sealed this run. Preserve the user message
                    # by routing it to the next run instead of leaving an unobservable orphan.
                    effective_delivery = "next_run"
            initial_status = "pending"
            connection.execute(
                """INSERT INTO cowork_steering_messages(
                       id, run_id, conversation_id, content, source, source_wake_id,
                       requested_delivery, delivery, status, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(item_id),
                    str(run_id),
                    str(conversation_id),
                    normalized,
                    source,
                    None if source_wake_id is None else str(source_wake_id),
                    delivery,
                    effective_delivery,
                    initial_status,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM cowork_steering_messages WHERE id = ?", (str(item_id),)
            ).fetchone()
            assert row is not None
            if effective_delivery == "next_run" and terminal:
                self._promote_next_run_transaction(connection, run_id=run_id)
                row = connection.execute(
                    "SELECT * FROM cowork_steering_messages WHERE id = ?", (str(item_id),)
                ).fetchone()
                assert row is not None
            self._append_queue_record_transaction(
                connection,
                row=row,
                phase="enqueued",
            )
            return self._queued_message_record(row)

        return await self._write(operation)

    async def consume_pending_steering(self, *, run_id: UUID) -> list[Any]:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> list[Any]:
            rows = connection.execute(
                """SELECT * FROM cowork_steering_messages
                   WHERE run_id = ? AND delivery = 'steer' AND status = 'pending'
                   ORDER BY created_at, id""",
                (str(run_id),),
            ).fetchall()
            if rows:
                connection.executemany(
                    """UPDATE cowork_steering_messages
                       SET status = 'consumed', consumed_at = ? WHERE id = ? AND status = 'pending'""",
                    [(timestamp, row["id"]) for row in rows],
                )
                for row in rows:
                    self._append_queue_record_transaction(
                        connection,
                        row=row,
                        phase="consumed",
                    )
            return [
                self._queued_message_record(
                    row,
                    status="consumed",
                    consumed_at=cast(datetime, _datetime(timestamp)),
                )
                for row in rows
            ]

        return await self._write(operation)

    async def claim_follow_up_or_seal(
        self,
        *,
        run_id: UUID,
        worker_id: str,
    ) -> list[SteeringRecord]:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> list[SteeringRecord]:
            run = connection.execute(
                "SELECT status, worker_id FROM agent_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
            if run is None:
                raise RunNotFoundError(str(run_id))
            if str(run["status"]) != "executing" or str(run["worker_id"] or "") != worker_id:
                raise ValueError("只有当前 run 租约持有者可以封闭 follow-up 队列")
            rows = connection.execute(
                """SELECT * FROM cowork_steering_messages
                   WHERE run_id = ? AND delivery IN ('steer','follow_up')
                     AND status = 'pending'
                   ORDER BY created_at, id""",
                (str(run_id),),
            ).fetchall()
            if rows:
                connection.executemany(
                    """UPDATE cowork_steering_messages
                       SET status = 'consumed', consumed_at = ?
                       WHERE id = ? AND status = 'pending'""",
                    [(timestamp, row["id"]) for row in rows],
                )
                for row in rows:
                    self._append_queue_record_transaction(
                        connection,
                        row=row,
                        phase="consumed",
                    )
                return [
                    self._queued_message_record(
                        row,
                        status="consumed",
                        consumed_at=cast(datetime, _datetime(timestamp)),
                    )
                    for row in rows
                ]
            connection.execute(
                """INSERT INTO cowork_run_queue_state(run_id, follow_up_open, sealed_at)
                   VALUES (?, 0, ?)
                   ON CONFLICT(run_id) DO UPDATE SET follow_up_open = 0, sealed_at = excluded.sealed_at""",
                (str(run_id), timestamp),
            )
            return []

        return await self._write(operation)

    async def cancel_queued_message(
        self,
        *,
        message_id: UUID,
        conversation_id: UUID,
    ) -> bool:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                """SELECT run_id, delivery, status FROM cowork_steering_messages
                   WHERE id = ? AND conversation_id = ? AND status IN ('pending','ready')""",
                (str(message_id), str(conversation_id)),
            ).fetchone()
            if row is None:
                return False
            changed = connection.execute(
                """UPDATE cowork_steering_messages
                   SET status = 'cancelled', cancelled_at = ?
                   WHERE id = ? AND status IN ('pending','ready')""",
                (timestamp, str(message_id)),
            ).rowcount
            if changed != 1:
                return False
            if str(row["delivery"]) == "next_run" and str(row["status"]) == "ready":
                self._promote_next_run_transaction(
                    connection,
                    run_id=UUID(str(row["run_id"])),
                )
            full_row = connection.execute(
                "SELECT * FROM cowork_steering_messages WHERE id = ?",
                (str(message_id),),
            ).fetchone()
            assert full_row is not None
            self._append_queue_record_transaction(
                connection,
                row=full_row,
                phase="cancelled",
            )
            return True

        return await self._write(operation)

    async def list_ready_next_run_messages(self, *, limit: int = 100) -> list[SteeringRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("ready next-run limit 必须位于 1 到 1000")
        return await self._read(
            lambda connection: [
                self._queued_message_record(row)
                for row in connection.execute(
                    """SELECT * FROM cowork_steering_messages
                       WHERE delivery = 'next_run' AND status = 'ready'
                       ORDER BY created_at, id LIMIT ?""",
                    (limit,),
                ).fetchall()
            ]
        )

    async def consume_ready_next_run_message(
        self,
        *,
        message_id: UUID,
        launched_run_id: UUID,
    ) -> bool:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                """SELECT run_id, conversation_id FROM cowork_steering_messages
                   WHERE id = ? AND delivery = 'next_run' AND status = 'ready'""",
                (str(message_id),),
            ).fetchone()
            if row is None:
                return False
            launched = connection.execute(
                "SELECT conversation_id, status FROM agent_runs WHERE id = ?",
                (str(launched_run_id),),
            ).fetchone()
            if launched is None or str(launched["conversation_id"]) != str(row["conversation_id"]):
                raise ValueError("next-run delivery 与新 run 会话不匹配")
            changed = connection.execute(
                """UPDATE cowork_steering_messages
                   SET status = 'consumed', consumed_at = ?
                   WHERE id = ? AND status = 'ready'""",
                (timestamp, str(message_id)),
            ).rowcount
            if changed != 1:
                return False
            full_row = connection.execute(
                "SELECT * FROM cowork_steering_messages WHERE id = ?",
                (str(message_id),),
            ).fetchone()
            assert full_row is not None
            self._append_queue_record_transaction(
                connection,
                row=full_row,
                phase="consumed",
                extra={"launched_run_id": str(launched_run_id)},
            )
            # Preserve FIFO across more than one next_run message: the remaining tail now waits
            # for the run created from this head, and that run's terminal transaction promotes
            # exactly one successor.
            connection.execute(
                """UPDATE cowork_steering_messages SET run_id = ?
                   WHERE run_id = ? AND conversation_id = ?
                     AND delivery = 'next_run' AND status = 'pending'""",
                (str(launched_run_id), str(row["run_id"]), str(row["conversation_id"])),
            )
            if str(launched["status"]) in TERMINAL_RUN_STATUSES:
                self._promote_next_run_transaction(connection, run_id=launched_run_id)
            return True

        return await self._write(operation)

    async def create_inbox_item(
        self,
        *,
        run_id: UUID,
        conversation_id: UUID,
        kind: str,
        tool_call_id: str,
        plan_step_id: UUID,
        request: dict[str, Any],
    ) -> Any:
        item_id = uuid7()
        resume_token = uuid7()
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> Any:
            run = connection.execute(
                "SELECT unattended FROM agent_runs WHERE id = ? AND conversation_id = ?",
                (str(run_id), str(conversation_id)),
            ).fetchone()
            if run is None:
                raise LookupError(f"run 不存在: {run_id}")
            connection.execute(
                """INSERT INTO cowork_inbox_items(
                       id, run_id, conversation_id, kind, status, resume_token, tool_call_id,
                       plan_step_id, request, unattended, created_at
                   ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
                (
                    str(item_id),
                    str(run_id),
                    str(conversation_id),
                    kind,
                    str(resume_token),
                    tool_call_id,
                    str(plan_step_id),
                    canonical_json(request),
                    int(run["unattended"]),
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM cowork_inbox_items WHERE id = ?", (str(item_id),)
            ).fetchone()
            assert row is not None
            return self._inbox_record(row, InboxRecord)

        return await self._write(operation)

    async def get_inbox_item(self, *, run_id: UUID, resume_token: UUID) -> Any | None:
        return await self._read(
            lambda connection: (
                None
                if (
                    row := connection.execute(
                        "SELECT * FROM cowork_inbox_items WHERE run_id = ? AND resume_token = ?",
                        (str(run_id), str(resume_token)),
                    ).fetchone()
                )
                is None
                else self._inbox_record(row, InboxRecord)
            )
        )

    async def list_unattended_inbox(
        self, *, include_resolved: bool = False, limit: int = 100
    ) -> list[Any]:
        if not 1 <= limit <= 200:
            raise ValueError("inbox limit 必须位于 1 到 200")

        def operation(connection: sqlite3.Connection) -> list[Any]:
            status_sql = "" if include_resolved else "AND inbox.status = 'pending'"
            rows = connection.execute(
                f"""SELECT inbox.*, runs.goal AS run_goal, runs.status AS run_status,
                           runs.schedule_id, schedules.title AS schedule_title
                    FROM cowork_inbox_items AS inbox
                    JOIN agent_runs AS runs ON runs.id = inbox.run_id
                    LEFT JOIN cowork_schedules AS schedules ON schedules.id = runs.schedule_id
                    WHERE inbox.unattended = 1 {status_sql}
                    ORDER BY (inbox.status = 'pending') DESC, inbox.created_at DESC, inbox.id DESC
                    LIMIT ?""",
                (limit,),
            ).fetchall()
            return [
                UnattendedInboxRecord(
                    item=self._inbox_record(row, InboxRecord),
                    run_goal=str(row["run_goal"]),
                    run_status=str(row["run_status"]),
                    schedule_id=None if row["schedule_id"] is None else UUID(row["schedule_id"]),
                    schedule_title=row["schedule_title"],
                )
                for row in rows
            ]

        return await self._read(operation)

    async def update_inbox_item(
        self, *, item_id: UUID, status: str, response: dict[str, Any]
    ) -> Any | None:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> Any | None:
            changed = connection.execute(
                """UPDATE cowork_inbox_items SET status = ?, response = ?, responded_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (status, canonical_json(response), timestamp, str(item_id)),
            ).rowcount
            if changed != 1:
                return None
            row = connection.execute(
                "SELECT * FROM cowork_inbox_items WHERE id = ?", (str(item_id),)
            ).fetchone()
            assert row is not None
            return self._inbox_record(row, InboxRecord)

        return await self._write(operation)

    async def cancel_pending_interaction(self, *, run_id: UUID) -> None:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """UPDATE cowork_inbox_items
                   SET status = 'cancelled', response = ?, responded_at = ?
                   WHERE run_id = ? AND status = 'pending'""",
                (
                    canonical_json({"approved": False, "status": "cancelled"}),
                    timestamp,
                    str(run_id),
                ),
            )
            connection.execute(
                """UPDATE cowork_steering_messages
                   SET status = 'cancelled', cancelled_at = ?
                   WHERE run_id = ? AND delivery = 'steer' AND status = 'pending'""",
                (timestamp, str(run_id)),
            )

        await self._write(operation)

    # ---- Agent Teams / Board -----------------------------------------------
    async def list_team_events(
        self, *, team_id: UUID, after_sequence: int = 0, limit: int = 200
    ) -> list[TeamEventRecord]:
        if after_sequence < 0:
            raise ValueError("after_sequence 不能为负数")
        if limit <= 0 or limit > 2_000:
            raise ValueError("Team event limit 必须在 1..2000")
        return await self._read(
            lambda connection: [
                self._team_event_record(row)
                for row in connection.execute(
                    """SELECT * FROM cowork_team_events
                       WHERE team_id = ? AND sequence > ?
                       ORDER BY sequence LIMIT ?""",
                    (str(team_id), after_sequence, limit),
                ).fetchall()
            ]
        )

    async def verify_team_event_log(self, *, team_id: UUID) -> TeamEventVerification:
        def operation(connection: sqlite3.Connection) -> TeamEventVerification:
            connection.execute("BEGIN")
            try:
                rows, head = self._verified_team_event_rows(connection, team_id)
                return TeamEventVerification(
                    team_id=team_id,
                    valid=True,
                    event_count=len(rows),
                    head_sequence=int(head["last_sequence"]),
                    head_hash=str(head["head_hash"]),
                    verified_at=_now(),
                )
            finally:
                connection.rollback()

        return await self._read(operation)

    async def replay_team_event_projection(self, *, team_id: UUID) -> TeamProjectionSummaryRecord:
        def operation(connection: sqlite3.Connection) -> TeamProjectionSummaryRecord:
            connection.execute("BEGIN")
            try:
                return self._replay_team_event_projection_transaction(connection, team_id)
            finally:
                connection.rollback()

        return await self._read(operation)

    async def rebuild_team_event_projection(self, *, team_id: UUID) -> TeamProjectionSummaryRecord:
        def operation(connection: sqlite3.Connection) -> TeamProjectionSummaryRecord:
            projection = self._replay_team_event_projection_transaction(connection, team_id)
            connection.execute(
                """INSERT INTO cowork_team_event_projection_summaries(
                       team_id, watermark, head_hash, summary, rebuilt_at
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(team_id) DO UPDATE SET
                       watermark = excluded.watermark,
                       head_hash = excluded.head_hash,
                       summary = excluded.summary,
                       rebuilt_at = excluded.rebuilt_at""",
                (
                    str(team_id),
                    projection.watermark,
                    projection.head_hash,
                    canonical_json(projection.summary),
                    _iso(projection.rebuilt_at),
                ),
            )
            return projection

        return await self._write(operation)

    async def get_team_event_cursor(
        self, *, team_id: UUID, consumer: str
    ) -> TeamEventCursorRecord | None:
        normalized_consumer = consumer.strip()
        if not normalized_consumer:
            raise ValueError("Team event consumer 不能为空")

        def operation(connection: sqlite3.Connection) -> TeamEventCursorRecord | None:
            row = connection.execute(
                """SELECT * FROM cowork_team_event_cursors
                   WHERE team_id = ? AND consumer = ?""",
                (str(team_id), normalized_consumer),
            ).fetchone()
            if row is None:
                return None
            return TeamEventCursorRecord(
                team_id=team_id,
                consumer=normalized_consumer,
                last_sequence=int(row["last_sequence"]),
                last_event_hash=str(row["last_event_hash"]),
                updated_at=datetime.fromisoformat(str(row["updated_at"])).astimezone(UTC),
            )

        return await self._read(operation)

    async def advance_team_event_cursor(
        self,
        *,
        team_id: UUID,
        consumer: str,
        expected_sequence: int,
        event_sequence: int,
        event_hash: str,
    ) -> TeamEventCursorRecord:
        normalized_consumer = consumer.strip()
        if not normalized_consumer:
            raise ValueError("Team event consumer 不能为空")
        if expected_sequence < 0 or event_sequence != expected_sequence + 1:
            raise ValueError("Team event cursor 只能逐条、无跳号前进")
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> TeamEventCursorRecord:
            rows, _ = self._verified_team_event_rows(connection, team_id)
            target = next((row for row in rows if int(row["sequence"]) == event_sequence), None)
            if target is None or str(target["hash"]) != event_hash:
                raise TeamEventIntegrityError("cursor 目标 event/hash 不匹配")
            current = connection.execute(
                """SELECT * FROM cowork_team_event_cursors
                   WHERE team_id = ? AND consumer = ?""",
                (str(team_id), normalized_consumer),
            ).fetchone()
            current_sequence = 0 if current is None else int(current["last_sequence"])
            if current_sequence != expected_sequence:
                raise ValueError(
                    f"Team event cursor CAS 失败：当前为 {current_sequence}，"
                    f"期望 {expected_sequence}"
                )
            if current is None:
                connection.execute(
                    """INSERT INTO cowork_team_event_cursors(
                           team_id, consumer, last_sequence, last_event_hash, updated_at
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        str(team_id),
                        normalized_consumer,
                        event_sequence,
                        event_hash,
                        timestamp,
                    ),
                )
            else:
                changed = connection.execute(
                    """UPDATE cowork_team_event_cursors SET
                           last_sequence = ?, last_event_hash = ?, updated_at = ?
                       WHERE team_id = ? AND consumer = ? AND last_sequence = ?""",
                    (
                        event_sequence,
                        event_hash,
                        timestamp,
                        str(team_id),
                        normalized_consumer,
                        expected_sequence,
                    ),
                ).rowcount
                if changed != 1:  # pragma: no cover - 同进程写锁外的防御式 CAS
                    raise ValueError("Team event cursor CAS 失败")
            return TeamEventCursorRecord(
                team_id=team_id,
                consumer=normalized_consumer,
                last_sequence=event_sequence,
                last_event_hash=event_hash,
                updated_at=datetime.fromisoformat(timestamp).astimezone(UTC),
            )

        return await self._write(operation)

    async def claim_team_wake_deliveries(
        self,
        *,
        consumer: str,
        claim_owner: str,
        limit: int = 20,
        lease_seconds: int = 30,
    ) -> list[TeamWakeDeliveryRecord]:
        normalized_consumer = consumer.strip()
        normalized_owner = claim_owner.strip()
        if normalized_consumer != _TEAM_WAKE_CONSUMER:
            raise ValueError(f"Team wake outbox 只允许 consumer {_TEAM_WAKE_CONSUMER!r}")
        if not normalized_owner:
            raise ValueError("Team wake claim_owner 不能为空")
        if limit < 1 or limit > 200 or lease_seconds < 1 or lease_seconds > 3600:
            raise ValueError("Team wake claim limit/lease 无效")
        now = _now()
        now_iso = _iso(now)
        lease_until = _iso(now + timedelta(seconds=lease_seconds))

        def operation(connection: sqlite3.Connection) -> list[TeamWakeDeliveryRecord]:
            rows = connection.execute(
                """SELECT wake.* FROM cowork_team_wake_outbox AS wake
                   LEFT JOIN cowork_team_event_cursors AS cursor
                     ON cursor.team_id = wake.team_id AND cursor.consumer = ?
                   WHERE wake.event_sequence = COALESCE(cursor.last_sequence, 0) + 1
                     AND (wake.status = 'pending'
                          OR (wake.status = 'claimed' AND wake.claim_until < ?))
                   ORDER BY wake.created_at, wake.team_id, wake.event_sequence LIMIT ?""",
                (normalized_consumer, now_iso, limit),
            ).fetchall()
            claimed: list[TeamWakeDeliveryRecord] = []
            for row in rows:
                changed = connection.execute(
                    """UPDATE cowork_team_wake_outbox SET status = 'claimed',
                              claim_owner = ?, claim_until = ?, attempt_count = attempt_count + 1,
                              validation_outcome = NULL, validated_at = NULL,
                              last_error = NULL, updated_at = ?
                       WHERE id = ? AND (status = 'pending'
                              OR (status = 'claimed' AND claim_until < ?))""",
                    (
                        normalized_owner,
                        lease_until,
                        now_iso,
                        str(row["id"]),
                        now_iso,
                    ),
                ).rowcount
                if changed != 1:  # pragma: no cover - 进程外竞争由条件更新兜底
                    continue
                current = connection.execute(
                    "SELECT * FROM cowork_team_wake_outbox WHERE id = ?", (str(row["id"]),)
                ).fetchone()
                assert current is not None
                claimed.append(self._team_wake_delivery_record(current))
            return claimed

        return await self._write(operation)

    @staticmethod
    def _wake_receipt_is_intact(receipt: dict[str, Any]) -> bool:
        receipt_id = receipt.get("receipt_id")
        unsigned = {key: value for key, value in receipt.items() if key != "receipt_id"}
        return isinstance(receipt_id, str) and receipt_id == _json_sha256(unsigned)

    def _validate_worker_wake_authority_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        team: sqlite3.Row,
        task: sqlite3.Row,
    ) -> None:
        scope = json.loads(str(task["resource_scope"] or "[]"))
        if not isinstance(scope, list):
            raise TeamEventIntegrityError("Worker wake task scope 非法")
        grants: list[sqlite3.Row] = list(
            connection.execute(
                """SELECT grants.id AS grant_id, grants.capability, grants.expires_at,
                          roots.id AS root_id, roots.canonical_path
                   FROM capability_grants AS grants
                   JOIN session_roots AS roots ON roots.id = grants.session_root_id
                   WHERE grants.conversation_id = ? AND grants.revoked_at IS NULL
                     AND roots.enabled = 1""",
                (str(team["lead_conversation_id"]),),
            ).fetchall()
        )
        now = _now()

        def active_grant(path: str, capability: str) -> sqlite3.Row | None:
            target = Path(path)
            for grant in grants:
                expires_at = _datetime(grant["expires_at"])
                root = Path(str(grant["canonical_path"]))
                if (
                    str(grant["capability"]) == capability
                    and (expires_at is None or expires_at > now)
                    and (target == root or target.is_relative_to(root))
                ):
                    return grant
            return None

        write_scope = [
            item
            for item in scope
            if isinstance(item, dict) and item.get("access_mode") == "read_write"
        ]
        for item in scope:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise TeamEventIntegrityError("Worker wake task scope item 非法")
            capability = (
                "filesystem.write" if item.get("access_mode") == "read_write" else "filesystem.read"
            )
            if active_grant(str(item["path"]), capability) is None:
                raise ValueError(f"Worker wake 前 {capability} grant 已失效")
        if not write_scope:
            return

        raw_delegation = team["write_delegation_receipt"]
        raw_task_receipt = task["scope_receipt"]
        if raw_delegation is None or raw_task_receipt is None:
            raise ValueError("Worker write wake 缺少 active delegation/scope receipt")
        delegation = json.loads(str(raw_delegation))
        receipt = json.loads(str(raw_task_receipt))
        team_scope = json.loads(str(team["write_delegation_scope"] or "[]"))
        if (
            not isinstance(delegation, dict)
            or not isinstance(receipt, dict)
            or not isinstance(team_scope, list)
            or not self._wake_receipt_is_intact(delegation)
            or not self._wake_receipt_is_intact(receipt)
            or delegation.get("conversation_id") != str(team["lead_conversation_id"])
            or delegation.get("proposal_call_id") != str(team["proposal_call_id"])
            or delegation.get("scope_sha256") != _json_sha256({"resource_scope": team_scope})
            or receipt.get("team_id") != str(team["id"])
            or receipt.get("scope_sha256") != _json_sha256({"resource_scope": scope})
            or receipt.get("delegation_receipt_id") != delegation.get("receipt_id")
        ):
            raise ValueError("Worker write wake receipt 已失效或与当前 scope 不一致")
        chains = receipt.get("authorization_chain")
        if not isinstance(chains, list):
            raise ValueError("Worker write wake receipt 授权链不完整")
        chain_by_path = {str(item.get("path")): item for item in chains if isinstance(item, dict)}
        for item in write_scope:
            path = str(item["path"])
            grant = active_grant(path, "filesystem.write")
            chain = chain_by_path.get(path)
            if (
                grant is None
                or chain is None
                or chain.get("grant_id") != str(grant["grant_id"])
                or chain.get("root_id") != str(grant["root_id"])
            ):
                raise ValueError("Worker write wake grant identity 与 receipt 不一致")

    async def validate_team_wake_delivery(
        self, *, delivery_id: UUID, claim_owner: str
    ) -> Literal["deliver", "suppress"]:
        normalized_owner = claim_owner.strip()
        if not normalized_owner:
            raise ValueError("Team wake claim_owner 不能为空")
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> Literal["deliver", "suppress"]:
            row = connection.execute(
                "SELECT * FROM cowork_team_wake_outbox WHERE id = ?", (str(delivery_id),)
            ).fetchone()
            if (
                row is None
                or row["status"] != "claimed"
                or str(row["claim_owner"] or "") != normalized_owner
            ):
                raise ValueError("Team wake delivery 未由当前 worker claim")
            claim_until = _datetime(row["claim_until"])
            if claim_until is None or claim_until <= _now():
                raise ValueError("Team wake delivery claim 已过期")
            event = connection.execute(
                "SELECT * FROM cowork_team_events WHERE id = ?", (str(row["event_id"]),)
            ).fetchone()
            if (
                event is None
                or int(event["sequence"]) != int(row["event_sequence"])
                or str(event["hash"]) != str(row["event_hash"])
                or str(event["event_type"]) != str(row["event_type"])
                or canonical_json(json.loads(str(event["payload"])))
                != canonical_json(json.loads(str(row["payload"])))
            ):
                raise TeamEventIntegrityError("Team wake outbox 与 hash-bound event 不一致")
            cursor = connection.execute(
                """SELECT last_sequence FROM cowork_team_event_cursors
                   WHERE team_id = ? AND consumer = ?""",
                (str(row["team_id"]), _TEAM_WAKE_CONSUMER),
            ).fetchone()
            cursor_sequence = 0 if cursor is None else int(cursor["last_sequence"])
            if int(row["event_sequence"]) != cursor_sequence + 1:
                raise ValueError("Team wake delivery 不是 cursor 的下一条 event")

            outcome: Literal["deliver", "suppress"] = "suppress"
            if row["target_kind"] != "none":
                team = connection.execute(
                    "SELECT * FROM cowork_teams WHERE id = ?", (str(row["team_id"]),)
                ).fetchone()
                if team is None:
                    raise TeamEventIntegrityError("Team wake target Team 不存在")
                for dimension in ("model_calls", "tool_calls", "wall_ms"):
                    if int(team[f"budget_used_{dimension}"]) + int(
                        team[f"budget_reserved_{dimension}"]
                    ) > int(team[f"budget_max_{dimension}"]):
                        raise ValueError(f"Team wake 前 {dimension} budget 状态非法")
                if int(team["budget_used_assignments"]) > int(team["budget_max_assignments"]):
                    raise ValueError("Team wake 前 assignments budget 状态非法")
                payload = json.loads(str(row["payload"]))
                task_payload = payload.get("task") if isinstance(payload, dict) else None
                task_id = task_payload.get("id") if isinstance(task_payload, dict) else None
                task = (
                    None
                    if not isinstance(task_id, str)
                    else connection.execute(
                        "SELECT * FROM cowork_board_tasks WHERE id = ? AND team_id = ?",
                        (task_id, str(team["id"])),
                    ).fetchone()
                )
                if task is None:
                    raise TeamEventIntegrityError("Team wake event 缺少当前 Board task")
                if row["target_kind"] == "lead":
                    if str(row["target_id"]) != str(team["lead_conversation_id"]):
                        raise TeamEventIntegrityError("Team wake Lead target 已漂移")
                    if team["status"] == "archived":
                        # archived 是不可恢复终态；旧结果只需推进 feed，不能再启动/steer Lead。
                        outcome = "suppress"
                    elif team["status"] != "active":
                        raise ValueError("Team 非 active，暂不投递 Lead wake")
                    else:
                        outcome = "deliver"
                else:
                    expected_worker = str(payload.get("worker_id") or "")
                    if (
                        str(row["target_id"]) != expected_worker
                        or str(task["assignee_worker_id"] or "") != expected_worker
                    ):
                        raise TeamEventIntegrityError("Team wake Worker ownership 已漂移")
                    expected_status = (
                        "in_progress" if row["event_type"] == "board.task.assigned" else "open"
                    )
                    if str(task["status"]) != expected_status:
                        outcome = "suppress"
                    elif team["status"] == "archived":
                        # archived 是不可恢复的 lifecycle 终态；旧 assignment/rework 不能
                        # 永久占住本 Team 的 gap-free cursor。
                        outcome = "suppress"
                    else:
                        if team["status"] != "active":
                            raise ValueError("Team 非 active，暂不投递 Worker wake")
                        if row["event_type"] == "board.task.assigned":
                            reservation = connection.execute(
                                """SELECT 1 FROM cowork_team_budget_reservations
                                   WHERE team_id = ? AND task_id = ?
                                     AND assignment_call_id = ? AND status = 'active'""",
                                (
                                    str(team["id"]),
                                    str(task["id"]),
                                    str(task["assignment_call_id"] or ""),
                                ),
                            ).fetchone()
                            if reservation is None:
                                raise ValueError("Worker wake 缺少 active budget reservation")
                        elif int(team["budget_used_assignments"]) >= int(
                            team["budget_max_assignments"]
                        ):
                            raise ValueError("Worker rework wake 前 assignment budget 已耗尽")
                        try:
                            self._validate_worker_wake_authority_transaction(
                                connection, team=team, task=task
                            )
                        except TeamEventIntegrityError:
                            raise
                        except ValueError:
                            # assignment 绑定的是当时的授权。授权/receipt 已撤销或过期后，
                            # 即便未来重新 grant，也必须由 Lead 产生新 assignment event，
                            # 不能让旧 wake 复活或永久形成 HOL blocking。
                            outcome = "suppress"
                        else:
                            outcome = "deliver"
            connection.execute(
                """UPDATE cowork_team_wake_outbox SET validation_outcome = ?,
                          validated_at = ?, updated_at = ? WHERE id = ?""",
                (outcome, timestamp, timestamp, str(delivery_id)),
            )
            return outcome

        return await self._write(operation)

    async def ack_team_wake_delivery(
        self,
        *,
        delivery_id: UUID,
        consumer: str,
        claim_owner: str,
        delivery_receipt: str,
    ) -> TeamWakeDeliveryRecord:
        normalized_consumer = consumer.strip()
        normalized_owner = claim_owner.strip()
        normalized_receipt = delivery_receipt.strip()
        if normalized_consumer != _TEAM_WAKE_CONSUMER:
            raise ValueError(f"Team wake outbox 只允许 consumer {_TEAM_WAKE_CONSUMER!r}")
        if not normalized_owner or not normalized_receipt or len(normalized_receipt) > 1000:
            raise ValueError("Team wake ack identity/receipt 无效")
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> TeamWakeDeliveryRecord:
            row = connection.execute(
                "SELECT * FROM cowork_team_wake_outbox WHERE id = ?", (str(delivery_id),)
            ).fetchone()
            if (
                row is None
                or row["status"] != "claimed"
                or str(row["claim_owner"] or "") != normalized_owner
                or row["validation_outcome"] not in {"deliver", "suppress"}
            ):
                raise ValueError("Team wake delivery 尚未由当前 worker 验证")
            event = connection.execute(
                "SELECT * FROM cowork_team_events WHERE id = ?", (str(row["event_id"]),)
            ).fetchone()
            if event is None or str(event["hash"]) != str(row["event_hash"]):
                raise TeamEventIntegrityError("Team wake ack event/hash 不匹配")
            cursor = connection.execute(
                """SELECT * FROM cowork_team_event_cursors
                   WHERE team_id = ? AND consumer = ?""",
                (str(row["team_id"]), normalized_consumer),
            ).fetchone()
            expected = int(row["event_sequence"]) - 1
            current = 0 if cursor is None else int(cursor["last_sequence"])
            if current != expected:
                raise ValueError(f"Team wake cursor CAS 失败：当前 {current}，期望 {expected}")
            connection.execute(
                """UPDATE cowork_team_wake_outbox SET status = 'delivered',
                          delivery_receipt = ?, claim_owner = NULL, claim_until = NULL,
                          delivered_at = ?, updated_at = ?
                   WHERE id = ? AND status = 'claimed' AND claim_owner = ?""",
                (
                    normalized_receipt,
                    timestamp,
                    timestamp,
                    str(delivery_id),
                    normalized_owner,
                ),
            )
            if cursor is None:
                connection.execute(
                    """INSERT INTO cowork_team_event_cursors(
                           team_id, consumer, last_sequence, last_event_hash, updated_at
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        str(row["team_id"]),
                        normalized_consumer,
                        int(row["event_sequence"]),
                        str(row["event_hash"]),
                        timestamp,
                    ),
                )
            else:
                changed = connection.execute(
                    """UPDATE cowork_team_event_cursors SET last_sequence = ?,
                              last_event_hash = ?, updated_at = ?
                       WHERE team_id = ? AND consumer = ? AND last_sequence = ?""",
                    (
                        int(row["event_sequence"]),
                        str(row["event_hash"]),
                        timestamp,
                        str(row["team_id"]),
                        normalized_consumer,
                        expected,
                    ),
                ).rowcount
                if changed != 1:  # pragma: no cover - 写锁外防御式 CAS
                    raise ValueError("Team wake cursor CAS 失败")
            updated = connection.execute(
                "SELECT * FROM cowork_team_wake_outbox WHERE id = ?", (str(delivery_id),)
            ).fetchone()
            assert updated is not None
            return self._team_wake_delivery_record(updated)

        return await self._write(operation)

    async def release_team_wake_delivery(
        self, *, delivery_id: UUID, claim_owner: str, error: str
    ) -> TeamWakeDeliveryRecord:
        normalized_owner = claim_owner.strip()
        safe_error = " ".join(error.split())[:1000] or "delivery failed"
        if not normalized_owner:
            raise ValueError("Team wake claim_owner 不能为空")
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> TeamWakeDeliveryRecord:
            changed = connection.execute(
                """UPDATE cowork_team_wake_outbox SET status = 'pending',
                          claim_owner = NULL, claim_until = NULL,
                          validation_outcome = NULL, validated_at = NULL,
                          last_error = ?, updated_at = ?
                   WHERE id = ? AND status = 'claimed' AND claim_owner = ?""",
                (safe_error, timestamp, str(delivery_id), normalized_owner),
            ).rowcount
            if changed != 1:
                raise ValueError("Team wake delivery 未由当前 worker claim")
            row = connection.execute(
                "SELECT * FROM cowork_team_wake_outbox WHERE id = ?", (str(delivery_id),)
            ).fetchone()
            assert row is not None
            return self._team_wake_delivery_record(row)

        return await self._write(operation)

    async def create_team(
        self,
        *,
        lead_conversation_id: UUID,
        proposal_call_id: str,
        note: str,
        members: Sequence[dict[str, Any]],
        write_delegation_scope: Sequence[dict[str, str]] = (),
        write_delegation_receipt: dict[str, Any] | None = None,
        budget_limits: dict[str, int] | None = None,
        event_actor: str = "system:store",
        event_cause: str | None = None,
    ) -> tuple[TeamRecord, list[TeamWorkerRecord]]:
        """审批通过后原子创建 roster 与零 token 的空闲 Worker Session。"""

        timestamp = _iso()
        limits = _team_budget_limits(budget_limits)

        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[TeamRecord, list[TeamWorkerRecord]]:
            existing = connection.execute(
                "SELECT * FROM cowork_teams WHERE proposal_call_id = ?",
                (proposal_call_id,),
            ).fetchone()
            if existing is not None:
                team = self._team_record(existing)
                rows = connection.execute(
                    "SELECT * FROM cowork_team_workers WHERE team_id = ? ORDER BY created_at, id",
                    (str(team.id),),
                ).fetchall()
                return team, [self._team_worker_record(row) for row in rows]
            if (
                connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (str(lead_conversation_id),)
                ).fetchone()
                is None
            ):
                raise ConversationNotFoundError(str(lead_conversation_id))
            if (
                connection.execute(
                    "SELECT 1 FROM cowork_teams WHERE lead_conversation_id = ?",
                    (str(lead_conversation_id),),
                ).fetchone()
                is not None
            ):
                raise ValueError("当前 Lead 会话已经创建过 Agent Team")

            team_id = uuid7()
            connection.execute(
                """INSERT INTO cowork_teams(
                       id, lead_conversation_id, proposal_call_id, status, note,
                       write_delegation_scope, write_delegation_receipt,
                       budget_max_model_calls, budget_max_tool_calls,
                       budget_max_wall_ms, budget_max_assignments, created_at, updated_at
                   ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(team_id),
                    str(lead_conversation_id),
                    proposal_call_id,
                    note,
                    canonical_json(write_delegation_scope),
                    (
                        None
                        if write_delegation_receipt is None
                        else canonical_json(write_delegation_receipt)
                    ),
                    limits["model_calls"],
                    limits["tool_calls"],
                    limits["wall_ms"],
                    limits["assignments"],
                    timestamp,
                    timestamp,
                ),
            )
            workers: list[TeamWorkerRecord] = []
            for member in members:
                worker_id = uuid7()
                session_id = uuid7()
                connection.execute(
                    """INSERT INTO cowork_team_workers(
                           id, team_id, name, role, reason, session_id, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(worker_id),
                        str(team_id),
                        str(member["name"]),
                        str(member["role"]),
                        str(member.get("reason") or ""),
                        str(session_id),
                        timestamp,
                    ),
                )
                connection.execute(
                    """INSERT INTO cowork_team_worker_sessions(
                           id, team_id, worker_id, status, active_task_id, state,
                           created_at, updated_at
                       ) VALUES (?, ?, ?, 'idle', NULL, ?, ?, ?)""",
                    (
                        str(session_id),
                        str(team_id),
                        str(worker_id),
                        canonical_json(cast("dict[str, Any]", member["state"])),
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM cowork_team_workers WHERE id = ?", (str(worker_id),)
                ).fetchone()
                assert row is not None
                workers.append(self._team_worker_record(row))
            row = connection.execute(
                "SELECT * FROM cowork_teams WHERE id = ?", (str(team_id),)
            ).fetchone()
            assert row is not None
            snapshot = self._team_event_projection_snapshot(connection, row)
            event_payloads: list[tuple[str, dict[str, Any]]] = [
                (
                    "team.created",
                    {
                        "team": snapshot["team"],
                        "workers": snapshot["workers"],
                    },
                )
            ]
            team_summary = cast("dict[str, Any]", snapshot["team"])
            team_receipt = cast("dict[str, Any] | None", team_summary["write_delegation_receipt"])
            if team_receipt is not None:
                event_payloads.append(
                    (
                        "team.write_delegation_receipt_minted",
                        {"receipt": {"kind": "team_write", **team_receipt}},
                    )
                )
            self._append_team_events_transaction(
                connection,
                team_id=team_id,
                actor=event_actor,
                cause=event_cause or proposal_call_id,
                events=event_payloads,
                created_at=timestamp,
            )
            return self._team_record(row), workers

        return await self._write(operation)

    async def get_team_for_lead(self, *, lead_conversation_id: UUID) -> TeamRecord | None:
        return await self._read(
            lambda connection: (
                None
                if (
                    row := connection.execute(
                        "SELECT * FROM cowork_teams WHERE lead_conversation_id = ?",
                        (str(lead_conversation_id),),
                    ).fetchone()
                )
                is None
                else self._team_record(row)
            )
        )

    async def list_team_workers(self, *, team_id: UUID) -> list[TeamWorkerRecord]:
        return await self._read(
            lambda connection: [
                self._team_worker_record(row)
                for row in connection.execute(
                    "SELECT * FROM cowork_team_workers WHERE team_id = ? ORDER BY created_at, id",
                    (str(team_id),),
                ).fetchall()
            ]
        )

    async def manage_team(
        self,
        *,
        lead_conversation_id: UUID,
        action: Literal["pause", "resume", "archive", "revoke_write_delegation"],
        budget_limits: dict[str, int] | None = None,
        reason: str,
        event_actor: str,
        event_cause: str,
    ) -> TeamRecord:
        timestamp = _iso()
        normalized_reason = reason.strip()
        if event_actor != "human:user":
            raise ValueError("Team lifecycle 只能由已验证的人工批准路径执行")
        if not normalized_reason:
            raise ValueError("Team lifecycle 变更必须说明 reason")
        limits = None if budget_limits is None else _team_budget_limits(budget_limits)

        def operation(connection: sqlite3.Connection) -> TeamRecord:
            row = connection.execute(
                "SELECT * FROM cowork_teams WHERE lead_conversation_id = ?",
                (str(lead_conversation_id),),
            ).fetchone()
            if row is None:
                raise ValueError("当前 Lead 会话没有 Agent Team")
            current = str(row["status"])
            if current == "archived" and action != "archive":
                raise ValueError("archived Team 不可恢复或修改")
            events: list[tuple[str, dict[str, Any]]] = []
            if action == "pause":
                if current == "active":
                    connection.execute(
                        """UPDATE cowork_teams SET status = 'paused', pause_reason = ?,
                                  updated_at = ? WHERE id = ?""",
                        (normalized_reason, timestamp, str(row["id"])),
                    )
            elif action == "resume":
                if current not in {"active", "paused"}:
                    raise ValueError("只有 active/paused Team 可以 resume")
                if limits is not None:
                    required = {
                        "model_calls": int(row["budget_used_model_calls"])
                        + int(row["budget_reserved_model_calls"]),
                        "tool_calls": int(row["budget_used_tool_calls"])
                        + int(row["budget_reserved_tool_calls"]),
                        "wall_ms": int(row["budget_used_wall_ms"])
                        + int(row["budget_reserved_wall_ms"]),
                        "assignments": int(row["budget_used_assignments"]),
                    }
                    if any(limits[key] < required[key] for key in required):
                        raise ValueError("新 Team budget 不能低于已经使用或预留的额度")
                    connection.execute(
                        """UPDATE cowork_teams SET budget_max_model_calls = ?,
                                  budget_max_tool_calls = ?, budget_max_wall_ms = ?,
                                  budget_max_assignments = ? WHERE id = ?""",
                        (
                            limits["model_calls"],
                            limits["tool_calls"],
                            limits["wall_ms"],
                            limits["assignments"],
                            str(row["id"]),
                        ),
                    )
                connection.execute(
                    """UPDATE cowork_teams SET status = 'active', pause_reason = NULL,
                              updated_at = ? WHERE id = ?""",
                    (timestamp, str(row["id"])),
                )
            elif action == "revoke_write_delegation":
                if current not in {"active", "paused"}:
                    raise ValueError("当前 Team 不可撤销写委派")
                running = connection.execute(
                    """SELECT tasks.*, sessions.id AS session_id,
                              sessions.state AS session_state,
                              sessions.worker_id AS worker_id
                       FROM cowork_board_tasks AS tasks
                       JOIN cowork_team_worker_sessions AS sessions
                         ON sessions.active_task_id = tasks.id
                       WHERE tasks.team_id = ? AND tasks.status = 'in_progress'
                         AND sessions.status = 'running'""",
                    (str(row["id"]),),
                ).fetchall()
                for task in running:
                    scope = json.loads(str(task["resource_scope"] or "[]"))
                    has_write_scope = isinstance(scope, list) and any(
                        isinstance(item, dict) and item.get("access_mode") == "read_write"
                        for item in scope
                    )
                    if not has_write_scope:
                        continue
                    connection.execute(
                        """UPDATE cowork_board_tasks SET status = 'blocked', last_error = ?,
                                  updated_at = ? WHERE id = ?""",
                        (
                            f"Team write delegation revoked：{normalized_reason}",
                            timestamp,
                            str(task["id"]),
                        ),
                    )
                    connection.execute(
                        """UPDATE cowork_team_worker_sessions SET status = 'idle',
                                  active_task_id = NULL, updated_at = ? WHERE id = ?""",
                        (timestamp, str(task["session_id"])),
                    )
                    settlement = self._settle_team_budget_transaction(
                        connection, task_id=str(task["id"]), timestamp=timestamp
                    )
                    if settlement is not None:
                        events.append(("team.budget_settled", settlement))
                    updated_task = connection.execute(
                        "SELECT * FROM cowork_board_tasks WHERE id = ?",
                        (str(task["id"]),),
                    ).fetchone()
                    assert updated_task is not None
                    events.append(
                        (
                            "board.task.blocked",
                            {
                                "task": self._board_task_event_summary(updated_task),
                                "worker_id": str(task["worker_id"]),
                                "session_id": str(task["session_id"]),
                                "session_state_sha256": _json_sha256(
                                    json.loads(str(task["session_state"]))
                                ),
                                "write_delegation_revoked": True,
                            },
                        )
                    )
                connection.execute(
                    """UPDATE cowork_teams SET write_delegation_scope = '[]',
                              write_delegation_receipt = NULL, updated_at = ? WHERE id = ?""",
                    (timestamp, str(row["id"])),
                )
            elif action == "archive":
                if current != "archived":
                    running = connection.execute(
                        """SELECT tasks.*, sessions.id AS session_id,
                                  sessions.state AS session_state,
                                  sessions.worker_id AS worker_id
                           FROM cowork_board_tasks AS tasks
                           JOIN cowork_team_worker_sessions AS sessions
                             ON sessions.active_task_id = tasks.id
                           WHERE tasks.team_id = ? AND tasks.status = 'in_progress'
                             AND sessions.status = 'running'""",
                        (str(row["id"]),),
                    ).fetchall()
                    for task in running:
                        connection.execute(
                            """UPDATE cowork_board_tasks SET status = 'blocked', last_error = ?,
                                      updated_at = ? WHERE id = ?""",
                            (
                                f"Team archived：{normalized_reason}",
                                timestamp,
                                str(task["id"]),
                            ),
                        )
                        connection.execute(
                            """UPDATE cowork_team_worker_sessions SET status = 'idle',
                                      active_task_id = NULL, updated_at = ? WHERE id = ?""",
                            (timestamp, str(task["session_id"])),
                        )
                        settlement = self._settle_team_budget_transaction(
                            connection, task_id=str(task["id"]), timestamp=timestamp
                        )
                        if settlement is not None:
                            events.append(("team.budget_settled", settlement))
                        updated_task = connection.execute(
                            "SELECT * FROM cowork_board_tasks WHERE id = ?",
                            (str(task["id"]),),
                        ).fetchone()
                        assert updated_task is not None
                        events.append(
                            (
                                "board.task.blocked",
                                {
                                    "task": self._board_task_event_summary(updated_task),
                                    "worker_id": str(task["worker_id"]),
                                    "session_id": str(task["session_id"]),
                                    "session_state_sha256": _json_sha256(
                                        json.loads(str(task["session_state"]))
                                    ),
                                    "team_archived": True,
                                },
                            )
                        )
                    connection.execute(
                        """UPDATE cowork_teams SET status = 'archived', pause_reason = ?,
                                  write_delegation_scope = '[]',
                                  write_delegation_receipt = NULL, updated_at = ? WHERE id = ?""",
                        (normalized_reason, timestamp, str(row["id"])),
                    )
            else:  # pragma: no cover - Literal contract
                raise ValueError(f"未知 Team lifecycle action: {action}")
            updated = connection.execute(
                "SELECT * FROM cowork_teams WHERE id = ?", (str(row["id"]),)
            ).fetchone()
            assert updated is not None
            events.append(
                (
                    {
                        "pause": "team.paused",
                        "resume": "team.resumed",
                        "archive": "team.archived",
                        "revoke_write_delegation": "team.write_delegation_revoked",
                    }[action],
                    {
                        "action": action,
                        "reason": normalized_reason,
                        "team": self._team_event_summary(updated),
                    },
                )
            )
            self._append_team_events_transaction(
                connection,
                team_id=str(row["id"]),
                actor=event_actor,
                cause=event_cause,
                events=events,
                created_at=timestamp,
            )
            return self._team_record(updated)

        return await self._write(operation)

    async def create_board_task(
        self,
        *,
        lead_conversation_id: UUID,
        title: str,
        description: str,
        acceptance_criteria: str,
        resource_scope: Sequence[dict[str, str]],
        scope_receipt: dict[str, Any] | None = None,
        event_actor: str = "system:store",
        event_cause: str | None = None,
    ) -> BoardTaskRecord:
        task_id = uuid7()
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> BoardTaskRecord:
            team = connection.execute(
                """SELECT id FROM cowork_teams
                   WHERE lead_conversation_id = ? AND status = 'active'""",
                (str(lead_conversation_id),),
            ).fetchone()
            if team is None:
                raise ValueError("当前会话没有已批准且处于 active 的 Agent Team")
            connection.execute(
                """INSERT INTO cowork_board_tasks(
                       id, team_id, title, description, acceptance_criteria,
                       resource_scope, scope_receipt, status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                (
                    str(task_id),
                    str(team["id"]),
                    title,
                    description,
                    acceptance_criteria,
                    canonical_json(resource_scope),
                    None if scope_receipt is None else canonical_json(scope_receipt),
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM cowork_board_tasks WHERE id = ?", (str(task_id),)
            ).fetchone()
            assert row is not None
            task_summary = self._board_task_event_summary(row)
            event_payloads: list[tuple[str, dict[str, Any]]] = [
                ("board.task.created", {"task": task_summary})
            ]
            task_receipt = task_summary["scope_receipt"]
            if task_receipt is not None:
                event_payloads.append(
                    (
                        "board.task.scope_receipt_minted",
                        {
                            "task": task_summary,
                            "receipt": {
                                "kind": "task_scope",
                                "task_id": str(task_id),
                                **task_receipt,
                            },
                        },
                    )
                )
            self._append_team_events_transaction(
                connection,
                team_id=str(team["id"]),
                actor=event_actor,
                cause=event_cause or f"store:create_board_task:{task_id}",
                events=event_payloads,
                created_at=timestamp,
            )
            return self._board_task_record(row)

        return await self._write(operation)

    async def list_board_tasks(
        self,
        *,
        lead_conversation_id: UUID,
        status: str | None = None,
        assignee: str | None = None,
    ) -> list[BoardTaskRecord]:
        def operation(connection: sqlite3.Connection) -> list[BoardTaskRecord]:
            clauses = ["teams.lead_conversation_id = ?"]
            params: list[Any] = [str(lead_conversation_id)]
            if status is not None:
                clauses.append("tasks.status = ?")
                params.append(status)
            if assignee is not None:
                clauses.append("workers.name = ?")
                params.append(assignee)
            rows = connection.execute(
                """SELECT tasks.* FROM cowork_board_tasks AS tasks
                   JOIN cowork_teams AS teams ON teams.id = tasks.team_id
                   LEFT JOIN cowork_team_workers AS workers
                     ON workers.id = tasks.assignee_worker_id
                   WHERE """
                + " AND ".join(clauses)
                + " ORDER BY tasks.created_at, tasks.id",
                tuple(params),
            ).fetchall()
            return [self._board_task_record(row) for row in rows]

        return await self._read(operation)

    async def start_board_task(
        self,
        *,
        lead_conversation_id: UUID,
        task_id: UUID,
        worker_name: str,
        assignment_call_id: str,
        source_run_id: UUID | None = None,
        budget_reservation: dict[str, int] | None = None,
        event_actor: str = "system:store",
        event_cause: str | None = None,
    ) -> tuple[BoardTaskRecord, TeamWorkerRecord, TeamWorkerSessionRecord]:
        timestamp = _iso()
        if budget_reservation is not None:
            if set(budget_reservation) != {"model_calls", "tool_calls", "wall_ms"} or any(
                type(value) is not int or value < 0 for value in budget_reservation.values()
            ):
                raise ValueError("Team assignment budget reservation 无效")
            if budget_reservation["model_calls"] < 1 or budget_reservation["wall_ms"] < 1:
                raise ValueError("Team assignment 至少需要一次模型调用和正墙钟预留")

        def operation(connection: sqlite3.Connection) -> Any:
            row = connection.execute(
                """SELECT tasks.*, teams.lead_conversation_id
                   FROM cowork_board_tasks AS tasks
                   JOIN cowork_teams AS teams ON teams.id = tasks.team_id
                   WHERE tasks.id = ?""",
                (str(task_id),),
            ).fetchone()
            if row is None or str(row["lead_conversation_id"]) != str(lead_conversation_id):
                raise ValueError("Board task 不存在或不属于当前 Lead 会话")
            requested_budget = budget_reservation or {
                "model_calls": 11,
                "tool_calls": 26 if json.loads(str(row["resource_scope"] or "[]")) else 0,
                "wall_ms": 300_000,
            }
            team_row = connection.execute(
                "SELECT * FROM cowork_teams WHERE id = ?", (str(row["team_id"]),)
            ).fetchone()
            assert team_row is not None
            if team_row["status"] != "active":
                raise ValueError("只有 active Team 可以分配或恢复 Worker task")
            worker_row = connection.execute(
                """SELECT * FROM cowork_team_workers
                   WHERE team_id = ? AND name = ?""",
                (str(row["team_id"]), worker_name),
            ).fetchone()
            if worker_row is None:
                raise ValueError(f"团队中没有名为 {worker_name!r} 的 Worker")
            session_row = connection.execute(
                "SELECT * FROM cowork_team_worker_sessions WHERE worker_id = ?",
                (str(worker_row["id"]),),
            ).fetchone()
            assert session_row is not None

            same_assignment = (
                row["status"] == "in_progress"
                and row["assignment_call_id"] == assignment_call_id
                and row["assignee_worker_id"] == worker_row["id"]
                and session_row["status"] == "running"
                and session_row["active_task_id"] == str(task_id)
            )
            if same_assignment:
                return (
                    self._board_task_record(row),
                    self._team_worker_record(worker_row),
                    self._team_worker_session_record(session_row),
                )
            same_assignment_finished = (
                row["status"] in {"review", "done", "blocked"}
                and row["assignment_call_id"] == assignment_call_id
                and row["assignee_worker_id"] == worker_row["id"]
                and session_row["status"] == "idle"
            )
            if same_assignment_finished:
                return (
                    self._board_task_record(row),
                    self._team_worker_record(worker_row),
                    self._team_worker_session_record(session_row),
                )
            if row["status"] not in {"open", "blocked"}:
                raise ValueError("只有 open/blocked 的 Board task 可以分配")
            if session_row["status"] != "idle":
                raise ValueError(f"Worker {worker_name!r} 当前仍有任务在执行")

            dimensions = ("model_calls", "tool_calls", "wall_ms")
            exceeded: tuple[str, int, int] | None = None
            for dimension in dimensions:
                used = int(team_row[f"budget_used_{dimension}"])
                reserved = int(team_row[f"budget_reserved_{dimension}"])
                requested = requested_budget[dimension]
                limit = int(team_row[f"budget_max_{dimension}"])
                if used + reserved + requested > limit:
                    exceeded = (dimension, used + reserved + requested, limit)
                    break
            assignment_used = int(team_row["budget_used_assignments"])
            assignment_limit = int(team_row["budget_max_assignments"])
            if exceeded is None and assignment_used + 1 > assignment_limit:
                exceeded = ("assignments", assignment_used + 1, assignment_limit)
            if exceeded is not None:
                dimension, used, limit = exceeded
                reason = f"budget:{dimension}"
                connection.execute(
                    """UPDATE cowork_teams SET status = 'paused', pause_reason = ?,
                              updated_at = ? WHERE id = ?""",
                    (reason, timestamp, str(row["team_id"])),
                )
                paused = connection.execute(
                    "SELECT * FROM cowork_teams WHERE id = ?", (str(row["team_id"]),)
                ).fetchone()
                assert paused is not None
                self._append_team_events_transaction(
                    connection,
                    team_id=str(row["team_id"]),
                    actor=event_actor,
                    cause=event_cause or assignment_call_id,
                    events=[
                        (
                            "team.budget_exceeded",
                            {
                                "dimension": dimension,
                                "used_or_reserved": used,
                                "limit": limit,
                                "team": self._team_event_summary(paused),
                            },
                        )
                    ],
                    created_at=timestamp,
                )
                return ("budget_exceeded", dimension, used, limit)

            reservation_id = uuid7()
            connection.execute(
                """INSERT INTO cowork_team_budget_reservations(
                       id, team_id, task_id, assignment_call_id, status,
                       reserved_model_calls, reserved_tool_calls, reserved_wall_ms,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)""",
                (
                    str(reservation_id),
                    str(row["team_id"]),
                    str(task_id),
                    assignment_call_id,
                    requested_budget["model_calls"],
                    requested_budget["tool_calls"],
                    requested_budget["wall_ms"],
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """UPDATE cowork_teams SET
                       budget_reserved_model_calls = budget_reserved_model_calls + ?,
                       budget_reserved_tool_calls = budget_reserved_tool_calls + ?,
                       budget_reserved_wall_ms = budget_reserved_wall_ms + ?,
                       budget_used_assignments = budget_used_assignments + 1,
                       updated_at = ? WHERE id = ?""",
                (
                    requested_budget["model_calls"],
                    requested_budget["tool_calls"],
                    requested_budget["wall_ms"],
                    timestamp,
                    str(row["team_id"]),
                ),
            )

            connection.execute(
                """UPDATE cowork_board_tasks SET
                       status = 'in_progress', assignee_worker_id = ?, assignment_call_id = ?,
                       attempt_count = attempt_count + 1, completion_kind = 'pending',
                       updated_at = ?
                   WHERE id = ?""",
                (str(worker_row["id"]), assignment_call_id, timestamp, str(task_id)),
            )
            connection.execute(
                """UPDATE cowork_team_worker_sessions SET
                       status = 'running', active_task_id = ?, updated_at = ?
                   WHERE id = ?""",
                (str(task_id), timestamp, str(session_row["id"])),
            )
            task_row = connection.execute(
                "SELECT * FROM cowork_board_tasks WHERE id = ?", (str(task_id),)
            ).fetchone()
            current_session = connection.execute(
                "SELECT * FROM cowork_team_worker_sessions WHERE id = ?",
                (str(session_row["id"]),),
            ).fetchone()
            assert task_row is not None and current_session is not None
            reserved_team = connection.execute(
                "SELECT * FROM cowork_teams WHERE id = ?", (str(task_row["team_id"]),)
            ).fetchone()
            assert reserved_team is not None
            self._append_team_events_transaction(
                connection,
                team_id=str(task_row["team_id"]),
                actor=event_actor,
                cause=event_cause or assignment_call_id,
                events=[
                    (
                        "team.budget_reserved",
                        {
                            "task_id": str(task_id),
                            "assignment_call_id": assignment_call_id,
                            "source_run_id": (
                                None if source_run_id is None else str(source_run_id)
                            ),
                            "reservation_id": str(reservation_id),
                            "reserved": requested_budget,
                            "budget": self._team_budget_summary(reserved_team),
                        },
                    ),
                    (
                        "board.task.assigned",
                        {
                            "task": self._board_task_event_summary(task_row),
                            "worker_id": str(worker_row["id"]),
                            "worker_name": str(worker_row["name"]),
                            "session_id": str(current_session["id"]),
                            "session_status": str(current_session["status"]),
                            "lead_conversation_id": str(row["lead_conversation_id"]),
                            "source_run_id": (
                                None if source_run_id is None else str(source_run_id)
                            ),
                        },
                    ),
                ],
                created_at=timestamp,
            )
            return (
                self._board_task_record(task_row),
                self._team_worker_record(worker_row),
                self._team_worker_session_record(current_session),
            )

        result = await self._write(operation)
        if isinstance(result, tuple) and result and result[0] == "budget_exceeded":
            _, dimension, used, limit = result
            raise TeamBudgetExceededError(
                cast("TeamBudgetDimension", dimension), used=int(used), limit=int(limit)
            )
        return cast("tuple[BoardTaskRecord, TeamWorkerRecord, TeamWorkerSessionRecord]", result)

    async def validate_team_worker_execution(
        self, *, session_id: UUID, task_id: UUID
    ) -> tuple[TeamRecord, BoardTaskRecord, TeamWorkerRecord, TeamWorkerSessionRecord]:
        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[TeamRecord, BoardTaskRecord, TeamWorkerRecord, TeamWorkerSessionRecord]:
            session_row = connection.execute(
                "SELECT * FROM cowork_team_worker_sessions WHERE id = ?",
                (str(session_id),),
            ).fetchone()
            if (
                session_row is None
                or session_row["status"] != "running"
                or str(session_row["active_task_id"] or "") != str(task_id)
            ):
                raise ValueError("Worker Session 已不再执行这条 Board task")
            task_row = connection.execute(
                "SELECT * FROM cowork_board_tasks WHERE id = ?", (str(task_id),)
            ).fetchone()
            worker_row = connection.execute(
                "SELECT * FROM cowork_team_workers WHERE id = ?",
                (str(session_row["worker_id"]),),
            ).fetchone()
            team_row = connection.execute(
                "SELECT * FROM cowork_teams WHERE id = ?", (str(session_row["team_id"]),)
            ).fetchone()
            if task_row is None or worker_row is None or team_row is None:
                raise ValueError("Team Worker execution 关系不完整")
            if team_row["status"] != "active":
                raise ValueError("Team 已暂停或归档，Worker 必须在安全点停止")
            if (
                task_row["status"] != "in_progress"
                or str(task_row["team_id"]) != str(team_row["id"])
                or str(task_row["assignee_worker_id"] or "") != str(worker_row["id"])
                or str(worker_row["team_id"]) != str(team_row["id"])
            ):
                raise ValueError("Board task ownership/status 已变化，Worker 必须停止")
            reservation = connection.execute(
                """SELECT 1 FROM cowork_team_budget_reservations
                   WHERE team_id = ? AND task_id = ? AND assignment_call_id = ?
                     AND status = 'active'""",
                (
                    str(team_row["id"]),
                    str(task_id),
                    str(task_row["assignment_call_id"] or ""),
                ),
            ).fetchone()
            if reservation is None:
                raise ValueError("Worker assignment 缺少 active Team budget reservation")
            return (
                self._team_record(team_row),
                self._board_task_record(task_row),
                self._team_worker_record(worker_row),
                self._team_worker_session_record(session_row),
            )

        return await self._read(operation)

    async def charge_team_budget(
        self,
        *,
        session_id: UUID,
        task_id: UUID,
        dimension: TeamBudgetDimension,
        amount: int,
        event_actor: str,
        event_cause: str,
    ) -> TeamBudgetReservationRecord:
        if dimension == "assignments":
            raise ValueError("assignment budget 在分配事务内结算")
        if type(amount) is not int or amount <= 0:
            raise ValueError("Team budget charge amount 必须为正整数")
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> Any:
            session_row = connection.execute(
                """SELECT * FROM cowork_team_worker_sessions
                   WHERE id = ? AND status = 'running' AND active_task_id = ?""",
                (str(session_id), str(task_id)),
            ).fetchone()
            if session_row is None:
                raise ValueError("Worker Session 已不再执行这条 Board task")
            task_row = connection.execute(
                "SELECT * FROM cowork_board_tasks WHERE id = ?", (str(task_id),)
            ).fetchone()
            team_row = connection.execute(
                "SELECT * FROM cowork_teams WHERE id = ?", (str(session_row["team_id"]),)
            ).fetchone()
            if task_row is None or team_row is None or team_row["status"] != "active":
                raise ValueError("Team/task 已不再允许 Worker 消耗预算")
            reservation = connection.execute(
                """SELECT * FROM cowork_team_budget_reservations
                   WHERE team_id = ? AND task_id = ? AND assignment_call_id = ?
                     AND status = 'active'""",
                (
                    str(team_row["id"]),
                    str(task_id),
                    str(task_row["assignment_call_id"] or ""),
                ),
            ).fetchone()
            if reservation is None:
                raise ValueError("Worker assignment 缺少 active Team budget reservation")
            remaining = int(reservation[f"reserved_{dimension}"]) - int(
                reservation[f"used_{dimension}"]
            )
            if amount > remaining:
                used = int(team_row[f"budget_used_{dimension}"]) + amount
                limit = int(team_row[f"budget_max_{dimension}"])
                connection.execute(
                    """UPDATE cowork_teams SET status = 'paused', pause_reason = ?,
                              updated_at = ? WHERE id = ?""",
                    (f"budget:{dimension}", timestamp, str(team_row["id"])),
                )
                paused = connection.execute(
                    "SELECT * FROM cowork_teams WHERE id = ?", (str(team_row["id"]),)
                ).fetchone()
                assert paused is not None
                self._append_team_events_transaction(
                    connection,
                    team_id=str(team_row["id"]),
                    actor=event_actor,
                    cause=event_cause,
                    events=[
                        (
                            "team.budget_exceeded",
                            {
                                "dimension": dimension,
                                "used_or_reserved": used,
                                "limit": limit,
                                "task_id": str(task_id),
                                "team": self._team_event_summary(paused),
                            },
                        )
                    ],
                    created_at=timestamp,
                )
                return ("budget_exceeded", dimension, used, limit)
            connection.execute(
                f"""UPDATE cowork_team_budget_reservations SET
                           used_{dimension} = used_{dimension} + ?, updated_at = ?
                     WHERE id = ?""",
                (amount, timestamp, str(reservation["id"])),
            )
            connection.execute(
                f"""UPDATE cowork_teams SET
                           budget_reserved_{dimension} = budget_reserved_{dimension} - ?,
                           budget_used_{dimension} = budget_used_{dimension} + ?,
                           updated_at = ? WHERE id = ?""",
                (amount, amount, timestamp, str(team_row["id"])),
            )
            updated_reservation = connection.execute(
                "SELECT * FROM cowork_team_budget_reservations WHERE id = ?",
                (str(reservation["id"]),),
            ).fetchone()
            updated_team = connection.execute(
                "SELECT * FROM cowork_teams WHERE id = ?", (str(team_row["id"]),)
            ).fetchone()
            assert updated_reservation is not None and updated_team is not None
            self._append_team_events_transaction(
                connection,
                team_id=str(team_row["id"]),
                actor=event_actor,
                cause=event_cause,
                events=[
                    (
                        "team.budget_consumed",
                        {
                            "task_id": str(task_id),
                            "reservation_id": str(reservation["id"]),
                            "dimension": dimension,
                            "amount": amount,
                            "budget": self._team_budget_summary(updated_team),
                        },
                    )
                ],
                created_at=timestamp,
            )
            return self._team_budget_reservation_record(updated_reservation)

        result = await self._write(operation)
        if isinstance(result, tuple) and result and result[0] == "budget_exceeded":
            _, failed_dimension, used, limit = result
            raise TeamBudgetExceededError(
                cast("TeamBudgetDimension", failed_dimension),
                used=int(used),
                limit=int(limit),
            )
        return cast("TeamBudgetReservationRecord", result)

    async def begin_team_worker_tool_attempt(
        self,
        *,
        session_id: UUID,
        task_id: UUID,
        tool_call_id: str,
        tool_name: str,
        effect: str,
        retry_safe: bool,
        arguments_sha256: str,
        event_actor: str,
        event_cause: str,
    ) -> TeamWorkerToolAttemptRecord:
        if not tool_call_id.strip() or len(tool_call_id) > 500 or not tool_name.strip():
            raise ValueError("Team Worker tool identity 无效")
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> TeamWorkerToolAttemptRecord:
            session_row = connection.execute(
                """SELECT * FROM cowork_team_worker_sessions
                   WHERE id = ? AND status = 'running' AND active_task_id = ?""",
                (str(session_id), str(task_id)),
            ).fetchone()
            if session_row is None:
                raise ValueError("Worker Session 已不再执行这条 Board task")
            task_row = connection.execute(
                "SELECT * FROM cowork_board_tasks WHERE id = ?", (str(task_id),)
            ).fetchone()
            team_row = connection.execute(
                "SELECT * FROM cowork_teams WHERE id = ?", (str(session_row["team_id"]),)
            ).fetchone()
            if (
                task_row is None
                or team_row is None
                or team_row["status"] != "active"
                or task_row["status"] != "in_progress"
                or str(task_row["assignee_worker_id"] or "") != str(session_row["worker_id"])
            ):
                raise ValueError("Team/task 已不再允许 Worker 启动工具")
            existing = connection.execute(
                """SELECT * FROM cowork_team_worker_tool_attempts
                   WHERE session_id = ? AND task_id = ? AND tool_call_id = ?""",
                (str(session_id), str(task_id), tool_call_id),
            ).fetchone()
            event_type = "team.worker_tool.started"
            if existing is None:
                attempt_id = UUID(str(uuid7()))
                connection.execute(
                    """INSERT INTO cowork_team_worker_tool_attempts(
                           id, team_id, session_id, task_id, tool_call_id, tool_name,
                           effect, retry_safe, status, arguments_sha256, attempt_count,
                           started_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_flight', ?, 1, ?, ?)""",
                    (
                        str(attempt_id),
                        str(team_row["id"]),
                        str(session_id),
                        str(task_id),
                        tool_call_id,
                        tool_name,
                        effect,
                        int(retry_safe),
                        arguments_sha256,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                if (
                    str(existing["tool_name"]) != tool_name
                    or str(existing["effect"]) != effect
                    or bool(existing["retry_safe"]) != retry_safe
                    or str(existing["arguments_sha256"]) != arguments_sha256
                ):
                    raise ValueError("同一 Worker tool_call_id 的工具或参数发生变化")
                if existing["status"] in {"succeeded", "failed", "unknown"}:
                    return self._team_worker_tool_attempt_record(existing)
                if not retry_safe:
                    connection.execute(
                        """UPDATE cowork_team_worker_tool_attempts SET status = 'unknown',
                                  finished_at = ?, updated_at = ? WHERE id = ?""",
                        (timestamp, timestamp, str(existing["id"])),
                    )
                    event_type = "team.worker_tool.unknown"
                else:
                    connection.execute(
                        """UPDATE cowork_team_worker_tool_attempts SET
                                  attempt_count = attempt_count + 1, started_at = ?,
                                  updated_at = ? WHERE id = ?""",
                        (timestamp, timestamp, str(existing["id"])),
                    )
                    event_type = "team.worker_tool.retried"
                attempt_id = UUID(str(existing["id"]))
            current = connection.execute(
                "SELECT * FROM cowork_team_worker_tool_attempts WHERE id = ?",
                (str(attempt_id),),
            ).fetchone()
            assert current is not None
            self._append_team_events_transaction(
                connection,
                team_id=str(team_row["id"]),
                actor=event_actor,
                cause=event_cause,
                events=[
                    (
                        event_type,
                        {
                            "attempt_id": str(attempt_id),
                            "task_id": str(task_id),
                            "session_id": str(session_id),
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "effect": effect,
                            "retry_safe": retry_safe,
                            "attempt_count": int(current["attempt_count"]),
                            "arguments_sha256": arguments_sha256,
                        },
                    )
                ],
                created_at=timestamp,
            )
            return self._team_worker_tool_attempt_record(current)

        return await self._write(operation)

    async def finish_team_worker_tool_attempt(
        self,
        *,
        attempt_id: UUID,
        status: Literal["succeeded", "failed"],
        result: dict[str, Any],
        effect_ref: str | None,
        authorization_receipt: dict[str, Any] | None,
        event_actor: str,
        event_cause: str,
    ) -> TeamWorkerToolAttemptRecord:
        safe_result = cast("dict[str, Any]", redact_persisted_tool_value(result))
        safe_effect_ref = (
            None if effect_ref is None else str(redact_persisted_tool_value(effect_ref))
        )
        result_json = canonical_json(safe_result)
        receipt_json = (
            None if authorization_receipt is None else canonical_json(authorization_receipt)
        )
        if len(result_json) > _TEAM_TOOL_ATTEMPT_RESULT_MAX_CHARS:
            raise ValueError("Team Worker tool attempt result 超过持久化上限")
        if receipt_json is not None and len(receipt_json) > _TEAM_TOOL_ATTEMPT_RECEIPT_MAX_CHARS:
            raise ValueError("Team Worker tool authorization receipt 超过持久化上限")
        if safe_effect_ref is not None and len(safe_effect_ref) > 2_000:
            raise ValueError("Team Worker tool effect_ref 超过持久化上限")
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> TeamWorkerToolAttemptRecord:
            row = connection.execute(
                "SELECT * FROM cowork_team_worker_tool_attempts WHERE id = ?",
                (str(attempt_id),),
            ).fetchone()
            if row is None:
                raise ValueError("Team Worker tool attempt 不存在")
            if row["status"] in {"succeeded", "failed"}:
                return self._team_worker_tool_attempt_record(row)
            if row["status"] != "in_flight":
                raise ValueError("结果未知的写工具 attempt 不得改写为成功或失败")
            connection.execute(
                """UPDATE cowork_team_worker_tool_attempts SET status = ?, result = ?,
                          effect_ref = ?, authorization_receipt = ?, finished_at = ?,
                          updated_at = ? WHERE id = ? AND status = 'in_flight'""",
                (
                    status,
                    result_json,
                    safe_effect_ref,
                    (receipt_json),
                    timestamp,
                    timestamp,
                    str(attempt_id),
                ),
            )
            updated = connection.execute(
                "SELECT * FROM cowork_team_worker_tool_attempts WHERE id = ?",
                (str(attempt_id),),
            ).fetchone()
            assert updated is not None
            self._append_team_events_transaction(
                connection,
                team_id=str(row["team_id"]),
                actor=event_actor,
                cause=event_cause,
                events=[
                    (
                        "team.worker_tool.finished",
                        {
                            "attempt_id": str(attempt_id),
                            "task_id": str(row["task_id"]),
                            "session_id": str(row["session_id"]),
                            "tool_call_id": str(row["tool_call_id"]),
                            "tool_name": str(row["tool_name"]),
                            "status": status,
                            "result_sha256": _json_sha256(safe_result),
                            "effect_ref": safe_effect_ref,
                            "authorization_receipt_sha256": (
                                None
                                if authorization_receipt is None
                                else _json_sha256(authorization_receipt)
                            ),
                        },
                    )
                ],
                created_at=timestamp,
            )
            return self._team_worker_tool_attempt_record(updated)

        return await self._write(operation)

    def _settle_team_budget_transaction(
        self, connection: sqlite3.Connection, *, task_id: str, timestamp: str
    ) -> dict[str, Any] | None:
        reservation = connection.execute(
            """SELECT * FROM cowork_team_budget_reservations
               WHERE task_id = ? AND status = 'active'""",
            (task_id,),
        ).fetchone()
        if reservation is None:
            return None
        remaining = {
            dimension: int(reservation[f"reserved_{dimension}"])
            - int(reservation[f"used_{dimension}"])
            for dimension in ("model_calls", "tool_calls", "wall_ms")
        }
        connection.execute(
            """UPDATE cowork_teams SET
                   budget_reserved_model_calls = budget_reserved_model_calls - ?,
                   budget_reserved_tool_calls = budget_reserved_tool_calls - ?,
                   budget_reserved_wall_ms = budget_reserved_wall_ms - ?,
                   updated_at = ? WHERE id = ?""",
            (
                remaining["model_calls"],
                remaining["tool_calls"],
                remaining["wall_ms"],
                timestamp,
                str(reservation["team_id"]),
            ),
        )
        connection.execute(
            """UPDATE cowork_team_budget_reservations SET status = 'settled',
                      settled_at = ?, updated_at = ? WHERE id = ?""",
            (timestamp, timestamp, str(reservation["id"])),
        )
        team = connection.execute(
            "SELECT * FROM cowork_teams WHERE id = ?", (str(reservation["team_id"]),)
        ).fetchone()
        assert team is not None
        return {
            "task_id": task_id,
            "reservation_id": str(reservation["id"]),
            "released": remaining,
            "budget": self._team_budget_summary(team),
        }

    async def save_team_worker_session(
        self,
        *,
        session_id: UUID,
        task_id: UUID,
        state: dict[str, Any],
        event_actor: str = "system:store",
        event_cause: str | None = None,
    ) -> TeamWorkerSessionRecord:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> TeamWorkerSessionRecord:
            valid = connection.execute(
                """SELECT 1 FROM cowork_team_worker_sessions AS sessions
                   JOIN cowork_board_tasks AS tasks ON tasks.id = sessions.active_task_id
                   JOIN cowork_teams AS teams ON teams.id = sessions.team_id
                   WHERE sessions.id = ? AND sessions.status = 'running'
                     AND sessions.active_task_id = ? AND tasks.status = 'in_progress'
                     AND tasks.assignee_worker_id = sessions.worker_id
                     AND teams.status = 'active'""",
                (str(session_id), str(task_id)),
            ).fetchone()
            if valid is None:
                raise ValueError("Team/task 已不再允许 Worker checkpoint")
            changed = connection.execute(
                """UPDATE cowork_team_worker_sessions SET state = ?, updated_at = ?
                   WHERE id = ? AND status = 'running' AND active_task_id = ?""",
                (canonical_json(state), timestamp, str(session_id), str(task_id)),
            ).rowcount
            if changed != 1:
                raise ValueError("Worker Session 已不再执行这条 Board task")
            row = connection.execute(
                "SELECT * FROM cowork_team_worker_sessions WHERE id = ?", (str(session_id),)
            ).fetchone()
            assert row is not None
            self._append_team_events_transaction(
                connection,
                team_id=str(row["team_id"]),
                actor=event_actor,
                cause=event_cause or f"store:worker_checkpoint:{task_id}",
                events=[
                    (
                        "team.worker_session.checkpointed",
                        {
                            "task_id": str(task_id),
                            "worker_id": str(row["worker_id"]),
                            "session_id": str(session_id),
                            "session_status": str(row["status"]),
                            "state_sha256": _json_sha256(state),
                        },
                    )
                ],
                created_at=timestamp,
            )
            return self._team_worker_session_record(row)

        return await self._write(operation)

    async def complete_board_task(
        self,
        *,
        session_id: UUID,
        task_id: UUID,
        state: dict[str, Any],
        worker_report: str,
        event_actor: str = "system:store",
        event_cause: str | None = None,
    ) -> BoardTaskRecord:
        return await self._finish_worker_task(
            session_id=session_id,
            task_id=task_id,
            state=state,
            task_status="review",
            worker_report=worker_report,
            last_error=None,
            session_status="idle",
            event_actor=event_actor,
            event_cause=event_cause,
        )

    async def fail_board_task(
        self,
        *,
        session_id: UUID,
        task_id: UUID,
        state: dict[str, Any],
        error: str,
        event_actor: str = "system:store",
        event_cause: str | None = None,
    ) -> BoardTaskRecord:
        if not error.strip() or len(error) > _TEAM_WORKER_LAST_ERROR_MAX_CHARS:
            raise ValueError("Team Worker last_error 为空或超过持久化上限")
        safe_error = str(redact_persisted_tool_value(error))
        return await self._finish_worker_task(
            session_id=session_id,
            task_id=task_id,
            state=state,
            task_status="blocked",
            worker_report=None,
            last_error=safe_error,
            session_status="idle",
            event_actor=event_actor,
            event_cause=event_cause,
        )

    async def _finish_worker_task(
        self,
        *,
        session_id: UUID,
        task_id: UUID,
        state: dict[str, Any],
        task_status: str,
        worker_report: str | None,
        last_error: str | None,
        session_status: str,
        event_actor: str,
        event_cause: str | None,
    ) -> BoardTaskRecord:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> BoardTaskRecord:
            session_row = connection.execute(
                """SELECT * FROM cowork_team_worker_sessions
                   WHERE id = ? AND status = 'running' AND active_task_id = ?""",
                (str(session_id), str(task_id)),
            ).fetchone()
            if session_row is None:
                raise ValueError("Worker Session 已不再执行这条 Board task")
            team_row = connection.execute(
                "SELECT * FROM cowork_teams WHERE id = ?", (str(session_row["team_id"]),)
            ).fetchone()
            if team_row is None:
                raise ValueError("Worker Team 不存在")
            if task_status == "review" and team_row["status"] != "active":
                raise ValueError("Team 已暂停或归档，不能提交新的 review")
            changed = connection.execute(
                """UPDATE cowork_board_tasks SET status = ?,
                          worker_report = COALESCE(?, worker_report), last_error = ?, updated_at = ?
                   WHERE id = ? AND status = 'in_progress'
                     AND assignee_worker_id = ?""",
                (
                    task_status,
                    worker_report,
                    last_error,
                    timestamp,
                    str(task_id),
                    str(session_row["worker_id"]),
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("Board task 状态已经变化，拒绝覆盖 Worker 结果")
            connection.execute(
                """UPDATE cowork_team_worker_sessions SET
                       status = ?, active_task_id = NULL, state = ?, updated_at = ?
                   WHERE id = ?""",
                (session_status, canonical_json(state), timestamp, str(session_id)),
            )
            row = connection.execute(
                "SELECT * FROM cowork_board_tasks WHERE id = ?", (str(task_id),)
            ).fetchone()
            assert row is not None
            settlement = self._settle_team_budget_transaction(
                connection, task_id=str(task_id), timestamp=timestamp
            )
            events: list[tuple[str, dict[str, Any]]] = [
                (
                    ("board.task.submitted" if task_status == "review" else "board.task.blocked"),
                    {
                        "task": self._board_task_event_summary(row),
                        "worker_id": str(session_row["worker_id"]),
                        "session_id": str(session_id),
                        "session_state_sha256": _json_sha256(state),
                    },
                )
            ]
            if settlement is not None:
                events.append(("team.budget_settled", settlement))
            self._append_team_events_transaction(
                connection,
                team_id=str(row["team_id"]),
                actor=event_actor,
                cause=event_cause or f"store:worker_finish:{task_id}",
                events=events,
                created_at=timestamp,
            )
            return self._board_task_record(row)

        return await self._write(operation)

    async def review_board_task(
        self,
        *,
        lead_conversation_id: UUID,
        task_id: UUID,
        accepted: bool,
        feedback: str,
        source_run_id: UUID | None = None,
        event_actor: str = "system:store",
        event_cause: str | None = None,
    ) -> BoardTaskRecord:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> BoardTaskRecord:
            row = connection.execute(
                """SELECT tasks.* FROM cowork_board_tasks AS tasks
                   JOIN cowork_teams AS teams ON teams.id = tasks.team_id
                   WHERE tasks.id = ? AND teams.lead_conversation_id = ?
                     AND teams.status = 'active'""",
                (str(task_id), str(lead_conversation_id)),
            ).fetchone()
            if row is None:
                raise ValueError("Board task 不存在或不属于当前 Lead 会话")
            if row["status"] != "review":
                raise ValueError("只有处于 review 的 Board task 可以验收")
            next_status = "done" if accepted else "open"
            completion_kind = "complete" if accepted else "pending"
            connection.execute(
                """UPDATE cowork_board_tasks SET
                       status = ?, completion_kind = ?, review_comment = ?,
                       last_rejection_comment = CASE WHEN ? THEN last_rejection_comment ELSE ? END,
                       updated_at = ? WHERE id = ?""",
                (
                    next_status,
                    completion_kind,
                    feedback,
                    int(accepted),
                    feedback,
                    timestamp,
                    str(task_id),
                ),
            )
            updated = connection.execute(
                "SELECT * FROM cowork_board_tasks WHERE id = ?", (str(task_id),)
            ).fetchone()
            assert updated is not None
            event_payloads: list[tuple[str, dict[str, Any]]] = [
                (
                    "board.task.reviewed",
                    {
                        "task": self._board_task_event_summary(updated),
                        "accepted": accepted,
                        "feedback": feedback,
                        "from_status": "review",
                        "to_status": next_status,
                    },
                )
            ]
            if not accepted:
                worker_id = updated["assignee_worker_id"]
                if worker_id is None:  # pragma: no cover - review 任务必有 assignee
                    raise ValueError("返工任务缺少原 Worker")
                event_payloads.append(
                    (
                        "board.task.rework_requested",
                        {
                            "task": self._board_task_event_summary(updated),
                            "worker_id": str(worker_id),
                            "feedback": feedback,
                            "lead_conversation_id": str(lead_conversation_id),
                            "source_run_id": (
                                None if source_run_id is None else str(source_run_id)
                            ),
                        },
                    )
                )
            self._append_team_events_transaction(
                connection,
                team_id=str(updated["team_id"]),
                actor=event_actor,
                cause=event_cause or f"store:review_board_task:{task_id}",
                events=event_payloads,
                created_at=timestamp,
            )
            return self._board_task_record(updated)

        return await self._write(operation)

    async def resolve_board_task(
        self,
        *,
        lead_conversation_id: UUID,
        task_id: UUID,
        resolution: Literal["accept_partial", "cancel"],
        reason: str,
        event_actor: str = "system:store",
        event_cause: str | None = None,
    ) -> BoardTaskRecord:
        """显式收束无法继续执行的任务；不会伪造一次 Worker review。"""

        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> BoardTaskRecord:
            row = connection.execute(
                """SELECT tasks.* FROM cowork_board_tasks AS tasks
                   JOIN cowork_teams AS teams ON teams.id = tasks.team_id
                   WHERE tasks.id = ? AND teams.lead_conversation_id = ?
                     AND teams.status = 'active'""",
                (str(task_id), str(lead_conversation_id)),
            ).fetchone()
            if row is None:
                raise ValueError("Board task 不存在或不属于当前 Lead 会话")
            if row["status"] not in {"open", "blocked", "review"}:
                raise ValueError("只有 open/blocked/review 的 Board task 可以部分接受或取消")
            status = "done" if resolution == "accept_partial" else "cancelled"
            completion_kind = "partial" if resolution == "accept_partial" else "cancelled"
            connection.execute(
                """UPDATE cowork_board_tasks SET status = ?, completion_kind = ?,
                          review_comment = ?, updated_at = ? WHERE id = ?""",
                (status, completion_kind, reason, timestamp, str(task_id)),
            )
            updated = connection.execute(
                "SELECT * FROM cowork_board_tasks WHERE id = ?", (str(task_id),)
            ).fetchone()
            assert updated is not None
            self._append_team_events_transaction(
                connection,
                team_id=str(updated["team_id"]),
                actor=event_actor,
                cause=event_cause or f"store:resolve_board_task:{task_id}",
                events=[
                    (
                        "board.task.resolved",
                        {
                            "task": self._board_task_event_summary(updated),
                            "resolution": resolution,
                            "reason": reason,
                            "from_status": str(row["status"]),
                            "to_status": status,
                        },
                    )
                ],
                created_at=timestamp,
            )
            return self._board_task_record(updated)

        return await self._write(operation)

    async def create_schedule(
        self,
        *,
        conversation_id: UUID,
        title: str,
        goal: str,
        schedule_kind: str,
        cron_expression: str | None,
        run_at: datetime | None,
        timezone: str,
        next_run_at: datetime,
    ) -> Any:
        schedule_id = uuid7()
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> Any:
            connection.execute(
                """INSERT INTO cowork_schedules(
                       id, conversation_id, title, goal, schedule_kind, cron_expression,
                       run_at, timezone, enabled, next_run_at, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (
                    str(schedule_id),
                    str(conversation_id),
                    title,
                    goal,
                    schedule_kind,
                    cron_expression,
                    None if run_at is None else _iso(run_at),
                    timezone,
                    _iso(next_run_at),
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM cowork_schedules WHERE id = ?", (str(schedule_id),)
            ).fetchone()
            assert row is not None
            return self._schedule_record(row, ScheduleRecord)

        return await self._write(operation)

    async def get_schedule(self, *, schedule_id: UUID) -> Any | None:
        return await self._read(
            lambda connection: (
                None
                if (
                    row := connection.execute(
                        "SELECT * FROM cowork_schedules WHERE id = ?", (str(schedule_id),)
                    ).fetchone()
                )
                is None
                else self._schedule_record(row, ScheduleRecord)
            )
        )

    async def list_schedules(self, *, limit: int = 100) -> list[Any]:
        if not 1 <= limit <= 200:
            raise ValueError("schedule limit 必须位于 1 到 200")

        def operation(connection: sqlite3.Connection) -> list[Any]:
            rows = connection.execute(
                """SELECT schedules.*,
                          runs.status AS last_run_status,
                          (SELECT COUNT(*) FROM cowork_inbox_items AS inbox
                           WHERE inbox.run_id = schedules.last_run_id
                             AND inbox.unattended = 1 AND inbox.status = 'pending') AS pending_inbox_count,
                          (SELECT label FROM session_roots AS roots
                           WHERE roots.conversation_id = schedules.conversation_id AND roots.enabled = 1
                           ORDER BY roots.created_at, roots.id LIMIT 1) AS workspace_label,
                          (SELECT canonical_path FROM session_roots AS roots
                           WHERE roots.conversation_id = schedules.conversation_id AND roots.enabled = 1
                           ORDER BY roots.created_at, roots.id LIMIT 1) AS workspace_path
                   FROM cowork_schedules AS schedules
                   LEFT JOIN agent_runs AS runs ON runs.id = schedules.last_run_id
                   ORDER BY schedules.enabled DESC, schedules.next_run_at IS NULL,
                            schedules.next_run_at, schedules.created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [
                ScheduleView(
                    schedule=self._schedule_record(row, ScheduleRecord),
                    last_run_status=row["last_run_status"],
                    pending_inbox_count=int(row["pending_inbox_count"]),
                    workspace_label=row["workspace_label"],
                    workspace_path=row["workspace_path"],
                )
                for row in rows
            ]

        return await self._read(operation)

    async def update_schedule_fields(
        self, *, schedule_id: UUID, values: dict[str, Any]
    ) -> Any | None:
        allowed = {
            "title",
            "goal",
            "enabled",
            "cron_expression",
            "run_at",
            "timezone",
            "next_run_at",
            "last_run_at",
            "last_run_id",
            "run_count",
            "skipped_count",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"未知 schedule 字段: {', '.join(sorted(unknown))}")

        def operation(connection: sqlite3.Connection) -> Any | None:
            if not values:
                row = connection.execute(
                    "SELECT * FROM cowork_schedules WHERE id = ?", (str(schedule_id),)
                ).fetchone()
                return None if row is None else self._schedule_record(row, ScheduleRecord)
            assignments: list[str] = []
            parameters: list[Any] = []
            for name, value in values.items():
                assignments.append(f"{name} = ?")
                if isinstance(value, datetime):
                    value = _iso(value)
                elif isinstance(value, UUID):
                    value = str(value)
                elif isinstance(value, bool):
                    value = int(value)
                parameters.append(value)
            assignments.append("updated_at = ?")
            parameters.extend([_iso(), str(schedule_id)])
            changed = connection.execute(
                f"UPDATE cowork_schedules SET {', '.join(assignments)} WHERE id = ?", parameters
            ).rowcount
            if changed != 1:
                return None
            row = connection.execute(
                "SELECT * FROM cowork_schedules WHERE id = ?", (str(schedule_id),)
            ).fetchone()
            assert row is not None
            return self._schedule_record(row, ScheduleRecord)

        return await self._write(operation)

    async def delete_schedule(self, *, schedule_id: UUID) -> bool:
        return await self._write(
            lambda connection: (
                connection.execute(
                    "DELETE FROM cowork_schedules WHERE id = ?", (str(schedule_id),)
                ).rowcount
                == 1
            )
        )

    async def upsert_plan_step(
        self,
        *,
        step_id: UUID,
        run_id: UUID,
        step_idx: int,
        description: str,
        tool: str | None,
        status: str,
    ) -> None:
        timestamp = _iso()
        await self._write(
            lambda connection: connection.execute(
                """INSERT INTO agent_plan_steps(
                       id, run_id, step_idx, description, tool, status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id, step_idx) DO NOTHING""",
                (
                    str(step_id),
                    str(run_id),
                    step_idx,
                    description,
                    tool,
                    status,
                    timestamp,
                    timestamp,
                ),
            )
        )

    async def list_plan_steps(self, *, run_id: UUID) -> list[Any]:
        return await self._read(
            lambda connection: [
                dict(row)
                for row in connection.execute(
                    """SELECT id, step_idx, description, tool, status
                       FROM agent_plan_steps WHERE run_id = ? ORDER BY step_idx""",
                    (str(run_id),),
                ).fetchall()
            ]
        )

    async def update_plan_step_status(self, *, run_id: UUID, step_id: UUID, status: str) -> None:
        await self._write(
            lambda connection: connection.execute(
                "UPDATE agent_plan_steps SET status = ?, updated_at = ? WHERE id = ? AND run_id = ?",
                (status, _iso(), str(step_id), str(run_id)),
            )
        )

    async def next_attempt_no(self, *, run_id: UUID, plan_step_id: UUID, node: str) -> int:
        return await self._read(
            lambda connection: (
                int(
                    connection.execute(
                        """SELECT COALESCE(MAX(attempt_no), 0) FROM agent_attempts
                       WHERE run_id = ? AND plan_step_id = ? AND node = ?""",
                        (str(run_id), str(plan_step_id), node),
                    ).fetchone()[0]
                )
                + 1
            )
        )

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
    ) -> UUID:
        attempt_id = uuid7()
        await self._write(
            lambda connection: connection.execute(
                """INSERT INTO agent_attempts(
                       id, run_id, plan_step_id, attempt_no, node, tool_name, tool_args,
                       tool_result, status, idempotency_key, latency_ms, tokens, error_model,
                       created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(attempt_id),
                    str(run_id),
                    None if plan_step_id is None else str(plan_step_id),
                    attempt_no,
                    node,
                    tool_name,
                    None if tool_args is None else canonical_json(tool_args),
                    None if tool_result is None else canonical_json(tool_result),
                    status,
                    idempotency_key,
                    latency_ms,
                    tokens,
                    error_model,
                    _iso(),
                ),
            )
        )
        return attempt_id

    async def set_run_waiting_human(self, *, run_id: UUID, worker_id: str) -> bool:
        return await self._write(
            lambda connection: (
                connection.execute(
                    """UPDATE agent_runs SET status = 'waiting_human', worker_id = NULL,
                          lease_until = NULL, heartbeat_at = NULL, updated_at = ?
                   WHERE id = ? AND worker_id = ? AND status = 'executing'""",
                    (_iso(), str(run_id), worker_id),
                ).rowcount
                == 1
            )
        )

    async def set_run_sleeping(self, *, run_id: UUID, worker_id: str, wake_at: datetime) -> bool:
        return await self._write(
            lambda connection: (
                connection.execute(
                    """UPDATE agent_runs SET status = 'sleeping', worker_id = NULL,
                          lease_until = NULL, heartbeat_at = NULL, wake_at = ?, updated_at = ?
                   WHERE id = ? AND worker_id = ? AND status = 'executing'""",
                    (_iso(wake_at), _iso(), str(run_id), worker_id),
                ).rowcount
                == 1
            )
        )

    async def schedule_run_retry(
        self,
        *,
        run_id: UUID,
        worker_id: str,
        max_recovery: int,
        base_delay_s: float,
        max_delay_s: float,
    ) -> tuple[int, datetime] | None:
        """从最新 checkpoint 安排一次持久化退避重试。

        状态、次数和 wake_at 在同一个 SQLite 写事务里更新。只有当前持租约的 worker
        能安排，且没有 checkpoint 时拒绝重投，避免从头重放已发生的外部副作用。
        """

        def operation(connection: sqlite3.Connection) -> tuple[int, datetime] | None:
            row = connection.execute(
                """SELECT recovery_count,
                          EXISTS(SELECT 1 FROM agent_checkpoints AS checkpoints
                                 WHERE checkpoints.run_id = runs.id) AS has_checkpoint
                   FROM agent_runs AS runs
                   WHERE runs.id = ? AND runs.worker_id = ? AND runs.status = 'executing'
                     AND runs.cancel_requested_at IS NULL""",
                (str(run_id), worker_id),
            ).fetchone()
            if row is None or not bool(row["has_checkpoint"]):
                return None
            recovery = int(row["recovery_count"])
            if recovery >= max_recovery:
                return None
            attempt = recovery + 1
            delay_s = min(max_delay_s, base_delay_s * (2**recovery))
            wake_at = _now() + timedelta(seconds=delay_s)
            updated = connection.execute(
                """UPDATE agent_runs SET status = 'sleeping', recovery_count = ?,
                          worker_id = NULL, lease_until = NULL, heartbeat_at = NULL,
                          wake_at = ?, updated_at = ?
                   WHERE id = ? AND worker_id = ? AND status = 'executing'
                     AND cancel_requested_at IS NULL""",
                (attempt, _iso(wake_at), _iso(), str(run_id), worker_id),
            ).rowcount
            return (attempt, wake_at) if updated == 1 else None

        return await self._write(operation)

    async def claim_due_sleeping_runs(self, *, now: datetime, limit: int) -> list[UUID]:
        """把到期的 sleeping run 转成 queued 并返回它们。

        状态翻转和取出在同一条 UPDATE 里完成，tick 重复执行不会把同一个 run 入队两次。
        """

        def _claim(connection: Any) -> list[UUID]:
            rows = connection.execute(
                """UPDATE agent_runs SET status = 'queued', wake_at = NULL, updated_at = ?
                   WHERE id IN (
                       SELECT id FROM agent_runs
                       WHERE status = 'sleeping' AND wake_at IS NOT NULL AND wake_at <= ?
                         AND cancel_requested_at IS NULL
                       ORDER BY wake_at LIMIT ?
                   )
                   RETURNING id""",
                (_iso(), _iso(now), limit),
            ).fetchall()
            return [UUID(row[0]) for row in rows]

        return await self._write(_claim)

    async def requeue_waiting_run(self, *, run_id: UUID) -> bool:
        return await self._write(
            lambda connection: (
                connection.execute(
                    """UPDATE agent_runs SET status = 'queued', worker_id = NULL,
                          lease_until = NULL, heartbeat_at = NULL, updated_at = ?
                   WHERE id = ? AND status = 'waiting_human' AND cancel_requested_at IS NULL""",
                    (_iso(), str(run_id)),
                ).rowcount
                == 1
            )
        )

    async def add_run_usage(self, *, run_id: UUID, used_tokens: int, used_calls: int) -> None:
        if used_tokens < 0 or used_calls < 0:
            raise ValueError("run 用量增量不能为负")
        if used_tokens == 0 and used_calls == 0:
            return
        await self._write(
            lambda connection: connection.execute(
                """UPDATE agent_runs SET used_tokens = used_tokens + ?, used_calls = used_calls + ?,
                          updated_at = ? WHERE id = ?""",
                (used_tokens, used_calls, _iso(), str(run_id)),
            )
        )

    @staticmethod
    def _promote_next_run_transaction(
        connection: sqlite3.Connection,
        *,
        run_id: UUID,
    ) -> None:
        # Failed/cancelled runs do not pass the successful outer follow-up boundary. Preserve
        # their queued user messages by carrying them into successor runs.
        connection.execute(
            """UPDATE cowork_steering_messages SET delivery = 'next_run'
               WHERE run_id = ? AND delivery IN ('steer','follow_up') AND status = 'pending'""",
            (str(run_id),),
        )
        connection.execute(
            """UPDATE cowork_steering_messages SET status = 'ready'
               WHERE id = (
                   SELECT id FROM cowork_steering_messages
                   WHERE run_id = ? AND delivery = 'next_run' AND status = 'pending'
                     AND NOT EXISTS (
                         SELECT 1 FROM cowork_steering_messages AS active
                         WHERE active.run_id = cowork_steering_messages.run_id
                           AND active.delivery = 'next_run' AND active.status = 'ready'
                     )
                   ORDER BY created_at, id LIMIT 1
               )""",
            (str(run_id),),
        )

    async def finish_run(
        self,
        *,
        run_id: UUID,
        status: str,
        worker_id: str | None = None,
        error: str | None = None,
        used_tokens: int = 0,
        used_calls: int = 0,
    ) -> bool:
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"不是终态: {status}")

        def operation(connection: sqlite3.Connection) -> bool:
            owner_sql = "" if worker_id is None else "AND worker_id = ?"
            parameters: list[Any] = [
                status,
                error,
                used_tokens,
                used_calls,
                _iso(),
                _iso(),
                str(run_id),
                status,
            ]
            if worker_id is not None:
                parameters.append(worker_id)
            changed = (
                connection.execute(
                    f"""UPDATE agent_runs SET status = ?, error = ?,
                               used_tokens = used_tokens + ?, used_calls = used_calls + ?,
                               finished_at = ?, lease_until = NULL, updated_at = ?
                        WHERE id = ? AND status <> ? {owner_sql}""",
                    parameters,
                ).rowcount
                == 1
            )
            if changed:
                self._promote_next_run_transaction(connection, run_id=run_id)
            return changed

        return await self._write(operation)

    async def finish_run_with_events(
        self,
        *,
        run_id: UUID,
        status: str,
        events: Sequence[RunEventDraft],
        worker_id: str | None = None,
        error: str | None = None,
        used_tokens: int = 0,
        used_calls: int = 0,
    ) -> tuple[bool, list[RunEvent]]:
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"不是终态: {status}")
        if used_tokens < 0 or used_calls < 0:
            raise ValueError("run 用量增量不能为负")

        def operation(connection: sqlite3.Connection) -> tuple[bool, list[RunEvent]]:
            owner_sql = "" if worker_id is None else "AND worker_id = ?"
            parameters: list[Any] = [
                status,
                error,
                used_tokens,
                used_calls,
                _iso(),
                _iso(),
                str(run_id),
                status,
            ]
            if worker_id is not None:
                parameters.append(worker_id)
            changed = (
                connection.execute(
                    f"""UPDATE agent_runs SET status = ?, error = ?,
                               used_tokens = used_tokens + ?, used_calls = used_calls + ?,
                               finished_at = ?, lease_until = NULL, heartbeat_at = NULL,
                               worker_id = NULL, updated_at = ?
                        WHERE id = ? AND status <> ? {owner_sql}""",
                    parameters,
                ).rowcount
                == 1
            )
            if not changed:
                return False, []
            self._promote_next_run_transaction(connection, run_id=run_id)
            return True, self._append_events_transaction(connection, run_id, events)

        return await self._write(operation)

    async def request_cancel(self, *, run_id: UUID) -> RunRecord:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> RunRecord:
            changed = connection.execute(
                """UPDATE agent_runs
                   SET cancel_requested_at = COALESCE(cancel_requested_at, ?),
                       status = CASE WHEN status IN ('queued','waiting_human','sleeping')
                                     THEN 'cancelled' ELSE status END,
                       finished_at = CASE WHEN status IN ('queued','waiting_human','sleeping')
                                          THEN ? ELSE finished_at END,
                       updated_at = ?
                   WHERE id = ?""",
                (timestamp, timestamp, timestamp, str(run_id)),
            ).rowcount
            if changed != 1:
                raise RunNotFoundError(str(run_id))
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
            assert row is not None
            self._append_session_record_transaction(
                connection,
                run_id=run_id,
                kind="abort_requested",
                operation_id=f"abort:{run_id}",
                phase="requested",
                payload={"source": "control_plane"},
            )
            if str(row["status"]) == "cancelled":
                self._promote_next_run_transaction(connection, run_id=run_id)
            return self._run_record(row)

        return await self._write(operation)

    async def reap_expired_runs(self, *, limit: int, max_recovery: int) -> dict[str, Any]:
        now = _iso()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            cancelled_rows = connection.execute(
                """SELECT id FROM agent_runs
                   WHERE cancel_requested_at IS NOT NULL
                     AND status NOT IN ('done','partial','failed','cancelled','budget_exceeded')
                     AND (status IN ('queued','waiting_human','sleeping')
                          OR (status = 'executing' AND (lease_until IS NULL OR lease_until < ?)))
                   ORDER BY updated_at LIMIT ?""",
                (now, limit),
            ).fetchall()
            cancelled = [UUID(row["id"]) for row in cancelled_rows]
            for run_id in cancelled:
                connection.execute(
                    """UPDATE agent_runs SET status = 'cancelled', finished_at = ?,
                              worker_id = NULL, lease_until = NULL, heartbeat_at = NULL,
                              updated_at = ? WHERE id = ?""",
                    (now, now, str(run_id)),
                )
                connection.execute(
                    """UPDATE cowork_inbox_items SET status = 'cancelled',
                              responded_at = COALESCE(responded_at, ?)
                       WHERE run_id = ? AND status = 'pending'""",
                    (now, str(run_id)),
                )

            expired = connection.execute(
                """SELECT runs.id, runs.recovery_count,
                          EXISTS(SELECT 1 FROM agent_checkpoints AS checkpoints
                                 WHERE checkpoints.run_id = runs.id) AS has_checkpoint,
                          EXISTS(
                              SELECT 1 FROM session_records AS started
                              WHERE started.run_id = runs.id AND started.kind = 'step_attempt'
                                AND started.phase = 'started'
                                AND NOT EXISTS (
                                    SELECT 1 FROM session_records AS terminal
                                    WHERE terminal.run_id = started.run_id
                                      AND terminal.operation_id = started.operation_id
                                      AND terminal.phase IN ('completed','failed')
                                )
                          ) AS has_open_model_attempt
                   FROM agent_runs AS runs
                   WHERE runs.cancel_requested_at IS NULL
                     AND runs.status NOT IN ('done','partial','failed','cancelled','budget_exceeded')
                     AND runs.lease_until IS NOT NULL AND runs.lease_until < ?
                   ORDER BY runs.lease_until LIMIT ?""",
                (now, limit),
            ).fetchall()
            recovered: list[tuple[UUID, int]] = []
            failed: list[UUID] = []
            for row in expired:
                run_id = UUID(row["id"])
                recovery = int(row["recovery_count"])
                if bool(row["has_open_model_attempt"]):
                    connection.execute(
                        """UPDATE agent_runs SET status = 'failed',
                                  error = 'model invocation outcome unknown; automatic replay refused',
                                  finished_at = ?, worker_id = NULL, lease_until = NULL,
                                  heartbeat_at = NULL, updated_at = ? WHERE id = ?""",
                        (now, now, str(run_id)),
                    )
                    failed.append(run_id)
                elif bool(row["has_checkpoint"]) and recovery < max_recovery:
                    attempt = recovery + 1
                    connection.execute(
                        """UPDATE agent_runs SET status = 'queued', recovery_count = ?,
                                  worker_id = NULL, lease_until = NULL, heartbeat_at = NULL,
                                  updated_at = ? WHERE id = ?""",
                        (attempt, now, str(run_id)),
                    )
                    recovered.append((run_id, attempt))
                else:
                    connection.execute(
                        """UPDATE agent_runs SET status = 'failed',
                                  error = 'worker 租约过期且无法安全恢复', finished_at = ?,
                                  worker_id = NULL, lease_until = NULL, heartbeat_at = NULL,
                                  updated_at = ? WHERE id = ?""",
                        (now, now, str(run_id)),
                    )
                    failed.append(run_id)
            for terminal_run_id in [*cancelled, *failed]:
                self._promote_next_run_transaction(connection, run_id=terminal_run_id)
            return {"cancelled": cancelled, "recovered": recovered, "failed": failed}

        return await self._write(operation)

    @staticmethod
    def _inbox_binding_record(row: sqlite3.Row) -> InboxBindingRecord:
        return InboxBindingRecord(
            id=UUID(row["id"]),
            name=str(row["name"]),
            platform=row["platform"],
            chat_id=row["chat_id"],
            connector_account_id=(
                None if row["connector_account_id"] is None else UUID(row["connector_account_id"])
            ),
            enabled=bool(row["enabled"]),
            created_at=cast(datetime, _datetime(row["created_at"])),
        )

    @staticmethod
    def _channel_subscription_record(row: sqlite3.Row) -> ChannelSubscriptionRecord:
        return ChannelSubscriptionRecord(
            id=UUID(row["id"]),
            conversation_id=UUID(row["conversation_id"]),
            platform=row["platform"],
            chat_id=str(row["chat_id"]),
            connector_account_id=(
                None if row["connector_account_id"] is None else UUID(row["connector_account_id"])
            ),
            created_at=cast(datetime, _datetime(row["created_at"])),
            revoked_at=_datetime(row["revoked_at"]),
        )

    @staticmethod
    def _thread_session_record(row: sqlite3.Row) -> ThreadSessionRecord:
        return ThreadSessionRecord(
            target=str(row["target"]),
            conversation_id=UUID(row["conversation_id"]),
            platform=row["platform"],
            chat_id=str(row["chat_id"]),
            thread_id=str(row["thread_id"]),
            created_at=cast(datetime, _datetime(row["created_at"])),
        )

    @staticmethod
    def _unrouted_record(row: sqlite3.Row) -> UnroutedRecord:
        return UnroutedRecord(
            id=UUID(row["id"]),
            kind=row["kind"],
            platform=row["platform"],
            chat_id=row["chat_id"],
            summary=str(row["summary"]),
            payload=json.loads(row["payload"]),
            created_at=cast(datetime, _datetime(row["created_at"])),
        )

    @staticmethod
    def _approval_rule_record(row: sqlite3.Row) -> ApprovalRuleRecord:
        return ApprovalRuleRecord(
            id=UUID(row["id"]),
            conversation_id=UUID(row["conversation_id"]),
            scope=row["scope"],
            schedule_id=None if row["schedule_id"] is None else UUID(row["schedule_id"]),
            tool=str(row["tool"]),
            match_kind=row["match_kind"],
            target=row["target"],
            created_by=str(row["created_by"]),
            revoked_at=_datetime(row["revoked_at"]),
            created_at=cast(datetime, _datetime(row["created_at"])),
        )

    @staticmethod
    def _grant_record(row: sqlite3.Row, record_type: Any) -> Any:
        return record_type(
            id=UUID(row["id"]),
            conversation_id=UUID(row["conversation_id"]),
            session_root_id=None
            if row["session_root_id"] is None
            else UUID(row["session_root_id"]),
            capability=row["capability"],
            resource_scope=row["resource_scope"],
            grant_source=str(row["grant_source"]),
            expires_at=_datetime(row["expires_at"]),
            revoked_at=_datetime(row["revoked_at"]),
            created_at=cast(datetime, _datetime(row["created_at"])),
            updated_at=cast(datetime, _datetime(row["updated_at"])),
        )

    @staticmethod
    def _artifact_record(row: sqlite3.Row, record_type: Any) -> Any:
        return record_type(
            id=UUID(row["id"]),
            conversation_id=UUID(row["conversation_id"]),
            run_id=None if row["run_id"] is None else UUID(row["run_id"]),
            session_root_id=None
            if row["session_root_id"] is None
            else UUID(row["session_root_id"]),
            kind=row["kind"],
            title=str(row["title"]),
            uri=str(row["uri"]),
            mime_type=row["mime_type"],
            meta=json.loads(row["meta"]),
            created_at=cast(datetime, _datetime(row["created_at"])),
            updated_at=cast(datetime, _datetime(row["updated_at"])),
        )

    @staticmethod
    def _attachment_record(row: sqlite3.Row, record_type: Any) -> Any:
        return record_type(
            id=UUID(row["id"]),
            conversation_id=UUID(row["conversation_id"]),
            message_id=None if row["message_id"] is None else UUID(row["message_id"]),
            run_id=None if row["run_id"] is None else UUID(row["run_id"]),
            kind=row["kind"],
            filename=str(row["filename"]),
            media_type=str(row["media_type"]),
            storage_path=str(row["storage_path"]),
            size_bytes=int(row["size_bytes"]),
            sha256=str(row["sha256"]),
            extracted_text=str(row["extracted_text"]),
        )

    # ---- 长期记忆 --------------------------------------------------------------

    @staticmethod
    def _assert_memory_save_policy(
        connection: sqlite3.Connection,
        snapshot: MemoryPolicySnapshot,
    ) -> None:
        """在持有 BEGIN IMMEDIATE 的同一事务里重查 any-off-wins 与 revision。"""

        owner = connection.execute(
            """SELECT save_enabled, revision FROM cowork_memory_owner_policy
               WHERE singleton_id = 1"""
        ).fetchone()
        owner_enabled = True if owner is None else bool(owner["save_enabled"])
        owner_revision = 0 if owner is None else int(owner["revision"])
        if not owner_enabled:
            raise MemoryPolicyConflictError("memory_save_disabled_by_owner")
        if owner_revision != snapshot.owner_revision:
            raise MemoryPolicyConflictError()

        if snapshot.conversation_id is None:
            if snapshot.conversation_revision is not None:
                raise MemoryPolicyConflictError()
            return
        if snapshot.conversation_revision is None:
            raise MemoryPolicyConflictError()
        if (
            connection.execute(
                "SELECT 1 FROM conversations WHERE id = ?",
                (str(snapshot.conversation_id),),
            ).fetchone()
            is None
        ):
            raise MemoryPolicyConflictError("memory_policy_conversation_missing")
        conversation = connection.execute(
            """SELECT save_mode, revision FROM cowork_memory_conversation_policies
               WHERE conversation_id = ?""",
            (str(snapshot.conversation_id),),
        ).fetchone()
        save_mode = "inherit" if conversation is None else str(conversation["save_mode"])
        revision = 0 if conversation is None else int(conversation["revision"])
        if save_mode == "off":
            raise MemoryPolicyConflictError("memory_save_disabled_for_conversation")
        if revision != snapshot.conversation_revision:
            raise MemoryPolicyConflictError()

    @staticmethod
    def _insert_cowork_memory_transaction(
        connection: sqlite3.Connection,
        *,
        memory_id: UUID,
        scope: str,
        conversation_id: UUID | None,
        workspace_path: str | None,
        key: str | None,
        content: str,
        source: str,
        timestamp: str,
        category: str,
        confidence: float,
        pinned: bool,
        valid_from: str,
        invalid_at: str | None = None,
        superseded_by: UUID | None = None,
        source_message_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> sqlite3.Row:
        connection.execute(
            """INSERT INTO cowork_memories(
                   id, scope, conversation_id, workspace_path, key, content, source,
                   created_at, updated_at, category, confidence, pinned, valid_from,
                   invalid_at, superseded_by, source_message_id, run_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(memory_id),
                scope,
                None if conversation_id is None else str(conversation_id),
                workspace_path,
                key,
                content,
                source,
                timestamp,
                timestamp,
                category,
                confidence,
                int(pinned),
                valid_from,
                invalid_at,
                None if superseded_by is None else str(superseded_by),
                None if source_message_id is None else str(source_message_id),
                None if run_id is None else str(run_id),
            ),
        )
        row = connection.execute(
            "SELECT * FROM cowork_memories WHERE id = ?", (str(memory_id),)
        ).fetchone()
        assert row is not None
        return cast(sqlite3.Row, row)

    async def remember_cowork_memory(
        self,
        *,
        scope: str,
        conversation_id: UUID | None,
        workspace_path: str | None,
        key: str | None,
        content: str,
        source: str,
        category: str = "fact",
        confidence: float = 1.0,
        pinned: bool = False,
        valid_from: datetime | None = None,
        source_message_id: UUID | None = None,
        run_id: UUID | None = None,
        policy_snapshot: MemoryPolicySnapshot,
    ) -> tuple[Any, Any]:
        memory_id = uuid7()
        timestamp = _iso()
        effective_from = _iso(valid_from)

        def operation(connection: sqlite3.Connection) -> tuple[Any, Any]:
            self._assert_memory_save_policy(connection, policy_snapshot)
            previous: Any = None
            if key is not None:
                row = connection.execute(
                    """SELECT * FROM cowork_memories
                       WHERE scope = ?
                         AND IFNULL(conversation_id, '') = IFNULL(?, '')
                         AND IFNULL(workspace_path, '') = IFNULL(?, '')
                         AND key = ? AND forgotten_at IS NULL AND invalid_at IS NULL""",
                    (
                        scope,
                        None if conversation_id is None else str(conversation_id),
                        workspace_path,
                        key,
                    ),
                ).fetchone()
                if row is not None:
                    previous = self._memory_record(row)
                    if previous.pinned:
                        # 置顶记忆是用户明确按住的那几条，模型的同 key 覆盖不能动它。
                        raise PinnedMemoryError(str(previous.id))
                    if (
                        previous.content == content
                        and previous.source == source
                        and previous.category == category
                        and previous.confidence == confidence
                        and previous.pinned == pinned
                    ):
                        return previous, None
                    # 先在同一事务里让旧版本退出 active unique index，再插入继承同 key
                    # 的 successor。INSERT 或回链失败会整体 rollback，不会丢当前版本。
                    connection.execute(
                        """UPDATE cowork_memories
                           SET invalid_at = ?, updated_at = ?
                           WHERE id = ?""",
                        (effective_from, timestamp, str(previous.id)),
                    )
            inserted = self._insert_cowork_memory_transaction(
                connection,
                memory_id=memory_id,
                scope=scope,
                conversation_id=conversation_id,
                workspace_path=workspace_path,
                key=key,
                content=content,
                source=source,
                timestamp=timestamp,
                category=category,
                confidence=confidence,
                pinned=pinned,
                valid_from=effective_from,
                source_message_id=source_message_id,
                run_id=run_id,
            )
            if previous is not None:
                connection.execute(
                    "UPDATE cowork_memories SET superseded_by = ? WHERE id = ?",
                    (str(memory_id), str(previous.id)),
                )
            return self._memory_record(inserted), previous

        return await self._write(operation)

    async def update_cowork_memory(
        self,
        *,
        memory_id: UUID,
        content: str | None,
        restore: bool,
        source: str,
        policy_snapshot: MemoryPolicySnapshot,
    ) -> tuple[Any, Any]:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> tuple[Any, Any]:
            self._assert_memory_save_policy(connection, policy_snapshot)
            row = connection.execute(
                "SELECT * FROM cowork_memories WHERE id = ?", (str(memory_id),)
            ).fetchone()
            if row is None:
                raise MemoryNotFoundError(str(memory_id))
            previous = self._memory_record(row)
            if restore:
                connection.execute(
                    """UPDATE cowork_memories SET forgotten_at = NULL, updated_at = ?
                       WHERE id = ?""",
                    (timestamp, str(memory_id)),
                )
                updated = connection.execute(
                    "SELECT * FROM cowork_memories WHERE id = ?", (str(memory_id),)
                ).fetchone()
                assert updated is not None
                return self._memory_record(updated), previous
            if content is None or content == previous.content:
                return previous, previous
            if not previous.active:
                raise MemoryNotFoundError(str(memory_id))
            if source == "agent" and previous.pinned:
                raise PinnedMemoryError(str(memory_id))

            successor_id = uuid7()
            connection.execute(
                """UPDATE cowork_memories SET invalid_at = ?, updated_at = ?
                   WHERE id = ? AND forgotten_at IS NULL AND invalid_at IS NULL""",
                (timestamp, timestamp, str(memory_id)),
            )
            inserted = self._insert_cowork_memory_transaction(
                connection,
                memory_id=successor_id,
                scope=previous.scope,
                conversation_id=previous.conversation_id,
                workspace_path=previous.workspace_path,
                key=previous.key,
                content=content,
                source=source,
                timestamp=timestamp,
                category=previous.category,
                confidence=previous.confidence,
                pinned=previous.pinned,
                valid_from=timestamp,
            )
            connection.execute(
                "UPDATE cowork_memories SET superseded_by = ? WHERE id = ?",
                (str(successor_id), str(memory_id)),
            )
            return self._memory_record(inserted), previous

        return await self._write(operation)

    async def apply_cowork_memory_operation(
        self,
        *,
        operation: Literal["ADD", "UPDATE", "DELETE", "NOOP"],
        category: str,
        fact: str,
        confidence: float,
        valid_from: datetime,
        source: Literal["agent", "user"],
        source_message_id: UUID | None,
        run_id: UUID | None,
        target_id: UUID | None,
        pinned: bool | None,
        scope: str,
        conversation_id: UUID | None,
        workspace_path: str | None,
        key: str | None,
        policy_snapshot: MemoryPolicySnapshot | None,
    ) -> CoworkMemoryMutation:
        """CAS gate 与 ADD/UPDATE/DELETE/NOOP 在一个 SQLite 写事务内完成。"""

        if policy_snapshot is None and operation != "DELETE":
            raise ValueError("只有 owner 明确隐私清理 DELETE 可以绕过 Memory save policy")
        timestamp = _iso()
        effective_from = _iso(valid_from)

        def mutate(connection: sqlite3.Connection) -> CoworkMemoryMutation:
            if policy_snapshot is not None:
                self._assert_memory_save_policy(connection, policy_snapshot)
            target_row = (
                None
                if target_id is None
                else connection.execute(
                    "SELECT * FROM cowork_memories WHERE id = ?", (str(target_id),)
                ).fetchone()
            )
            target = None if target_row is None else self._memory_record(target_row)

            if operation == "NOOP":
                if target_id is None:
                    return CoworkMemoryMutation(False, False, None)
                if target is None or not target.active:
                    raise MemoryNotFoundError(str(target_id))
                connection.execute(
                    """UPDATE cowork_memories
                       SET access_count = access_count + 1, last_used_at = ?
                       WHERE id = ?""",
                    (timestamp, str(target.id)),
                )
                updated = connection.execute(
                    "SELECT * FROM cowork_memories WHERE id = ?", (str(target.id),)
                ).fetchone()
                assert updated is not None
                return CoworkMemoryMutation(True, False, self._memory_record(updated), target)

            if operation in {"UPDATE", "DELETE"}:
                if target is None:
                    raise MemoryNotFoundError(str(target_id))
                if not target.active:
                    # Worker 在 mutation 成功后、job complete 前退出时可能重放同一决定。
                    # 相同 successor/DELETE 返回 unchanged，不能再制造第二条版本。
                    if operation == "UPDATE" and target.superseded_by is not None:
                        successor_row = connection.execute(
                            "SELECT * FROM cowork_memories WHERE id = ?",
                            (str(target.superseded_by),),
                        ).fetchone()
                        if successor_row is not None:
                            successor = self._memory_record(successor_row)
                            if successor.active and successor.content == fact:
                                return CoworkMemoryMutation(False, False, successor, target)
                    if operation == "DELETE" and target.superseded_by is None:
                        return CoworkMemoryMutation(False, False, target, target)
                    raise MemoryNotFoundError(str(target_id))
                if target.pinned and source == "agent":
                    raise PinnedMemoryError("置顶记忆不能被自动改写或失效")

            if operation == "DELETE":
                assert target is not None
                if target.valid_from is not None and valid_from < target.valid_from:
                    return CoworkMemoryMutation(False, False, target, target)
                connection.execute(
                    """UPDATE cowork_memories
                       SET invalid_at = ?, superseded_by = NULL, updated_at = ?
                       WHERE id = ?""",
                    (effective_from, timestamp, str(target.id)),
                )
                deleted = connection.execute(
                    "SELECT * FROM cowork_memories WHERE id = ?", (str(target.id),)
                ).fetchone()
                assert deleted is not None
                return CoworkMemoryMutation(True, True, self._memory_record(deleted), target)

            stale = (
                target is not None
                and target.valid_from is not None
                and valid_from < target.valid_from
            )
            write_scope = target.scope if target is not None else scope
            write_conversation = target.conversation_id if target is not None else conversation_id
            write_workspace = target.workspace_path if target is not None else workspace_path
            write_key = target.key if target is not None else key
            write_pinned = target.pinned if pinned is None and target is not None else bool(pinned)
            created_id = uuid7()

            if target is not None and not stale:
                # 先释放 active keyed unique slot；后续任一步失败会整笔 rollback。
                connection.execute(
                    """UPDATE cowork_memories SET invalid_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (effective_from, timestamp, str(target.id)),
                )
            inserted = self._insert_cowork_memory_transaction(
                connection,
                memory_id=created_id,
                scope=write_scope,
                conversation_id=write_conversation,
                workspace_path=write_workspace,
                key=write_key,
                content=fact,
                source=source,
                timestamp=timestamp,
                category=category,
                confidence=confidence,
                pinned=write_pinned,
                valid_from=effective_from,
                invalid_at=(
                    _iso(cast(datetime, target.valid_from))
                    if stale and target is not None
                    else None
                ),
                superseded_by=target.id if stale and target is not None else None,
                source_message_id=source_message_id,
                run_id=run_id,
            )
            if target is not None and not stale:
                connection.execute(
                    "UPDATE cowork_memories SET superseded_by = ? WHERE id = ?",
                    (str(created_id), str(target.id)),
                )
            return CoworkMemoryMutation(
                True,
                not stale,
                self._memory_record(inserted),
                target,
            )

        return await self._write(mutate)

    async def forget_cowork_memory(self, *, memory_id: UUID) -> Any | None:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> Any | None:
            changed = connection.execute(
                """UPDATE cowork_memories
                   SET forgotten_at = ?, updated_at = ?
                   WHERE id = ? AND forgotten_at IS NULL""",
                (timestamp, timestamp, str(memory_id)),
            ).rowcount
            if not changed:
                return None
            row = connection.execute(
                "SELECT * FROM cowork_memories WHERE id = ?", (str(memory_id),)
            ).fetchone()
            return self._memory_record(row)

        return await self._write(operation)

    async def get_cowork_memory(self, *, memory_id: UUID) -> Any | None:
        return await self._read(
            lambda connection: (
                None
                if (
                    row := connection.execute(
                        "SELECT * FROM cowork_memories WHERE id = ?", (str(memory_id),)
                    ).fetchone()
                )
                is None
                else self._memory_record(row)
            )
        )

    async def list_cowork_memories(
        self,
        *,
        conversation_id: UUID,
        workspace_paths: list[str],
        include_forgotten: bool,
        limit: int,
    ) -> list[Any]:
        placeholders = ",".join("?" for _ in workspace_paths) or "NULL"

        def operation(connection: sqlite3.Connection) -> list[Any]:
            rows = connection.execute(
                f"""SELECT * FROM cowork_memories
                    WHERE (? OR forgotten_at IS NULL)
                      AND (
                        scope = 'global'
                        OR (scope = 'conversation' AND conversation_id = ?)
                        OR (scope = 'workspace' AND workspace_path IN ({placeholders}))
                      )
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ?""",
                (
                    int(include_forgotten),
                    str(conversation_id),
                    *workspace_paths,
                    limit,
                ),
            ).fetchall()
            return [self._memory_record(row) for row in rows]

        return await self._read(operation)

    async def supersede_cowork_memory(
        self,
        *,
        memory_id: UUID,
        successor_id: UUID | None,
        invalid_at: datetime,
    ) -> Any | None:
        """让一条记忆失效，而不是覆盖它。

        ADR-0005 的时序有效性：`invalid_at` 记的是"从这一刻起不再成立"，不是"这一刻
        被删掉"。乱序到达的抽取结果（模型今天才提炼出上个月的偏好）也靠它落位——
        新的 invalid_at 只有比现有的更早才写，否则会把一条已经失效的记忆"复活"到
        更晚的时刻。
        """

        timestamp = _iso()
        moment = _iso(invalid_at)

        def operation(connection: sqlite3.Connection) -> Any | None:
            row = connection.execute(
                "SELECT * FROM cowork_memories WHERE id = ?", (str(memory_id),)
            ).fetchone()
            if row is None:
                return None
            if row["pinned"]:
                raise PinnedMemoryError(str(memory_id))
            existing = row["invalid_at"]
            if existing is not None and existing <= moment:
                return self._memory_record(row)
            connection.execute(
                """UPDATE cowork_memories
                   SET invalid_at = ?, superseded_by = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    moment,
                    None if successor_id is None else str(successor_id),
                    timestamp,
                    str(memory_id),
                ),
            )
            updated = connection.execute(
                "SELECT * FROM cowork_memories WHERE id = ?", (str(memory_id),)
            ).fetchone()
            return self._memory_record(updated)

        return await self._write(operation)

    async def set_cowork_memory_pinned(
        self,
        *,
        memory_id: UUID,
        pinned: bool,
        policy_snapshot: MemoryPolicySnapshot,
    ) -> Any | None:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> Any | None:
            self._assert_memory_save_policy(connection, policy_snapshot)
            changed = connection.execute(
                """UPDATE cowork_memories SET pinned = ?, updated_at = ?
                   WHERE id = ? AND forgotten_at IS NULL AND invalid_at IS NULL""",
                (int(pinned), timestamp, str(memory_id)),
            ).rowcount
            if not changed:
                return None
            row = connection.execute(
                "SELECT * FROM cowork_memories WHERE id = ?", (str(memory_id),)
            ).fetchone()
            assert row is not None
            return self._memory_record(row)

        return await self._write(operation)

    async def touch_cowork_memories(self, *, memory_ids: list[UUID]) -> None:
        """记一次命中。看板按 access_count 排"哪些记忆真的在起作用"。"""

        if not memory_ids:
            return
        timestamp = _iso()
        placeholders = ",".join("?" for _ in memory_ids)

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                f"""UPDATE cowork_memories
                    SET access_count = access_count + 1, last_used_at = ?
                    WHERE id IN ({placeholders})""",
                (timestamp, *(str(item) for item in memory_ids)),
            )

        await self._write(operation)

    async def list_cowork_memories_by_validity(self, *, active: bool, limit: int) -> list[Any]:
        """记忆面板的两个视图：当前生效 / 已失效的历史。"""

        clause = (
            "forgotten_at IS NULL AND invalid_at IS NULL" if active else "invalid_at IS NOT NULL"
        )
        order = "pinned DESC, valid_from DESC, id DESC" if active else "invalid_at DESC, id DESC"

        def operation(connection: sqlite3.Connection) -> list[Any]:
            rows = connection.execute(
                f"SELECT * FROM cowork_memories WHERE {clause} ORDER BY {order} LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._memory_record(row) for row in rows]

        return await self._read(operation)

    async def get_owner_memory_policy(self) -> OwnerMemoryPolicy:
        def operation(connection: sqlite3.Connection) -> OwnerMemoryPolicy:
            row = connection.execute(
                """SELECT save_enabled, recall_enabled, standing_rules, revision
                   FROM cowork_memory_owner_policy WHERE singleton_id = 1"""
            ).fetchone()
            if row is None:
                return OwnerMemoryPolicy()
            return OwnerMemoryPolicy(
                save_enabled=bool(row["save_enabled"]),
                recall_enabled=bool(row["recall_enabled"]),
                standing_rules=str(row["standing_rules"]),
                revision=int(row["revision"]),
            )

        return await self._read(operation)

    async def upsert_owner_memory_policy(
        self,
        *,
        save_enabled: bool,
        recall_enabled: bool,
        standing_rules: str,
        expected_revision: int,
    ) -> OwnerMemoryPolicy:
        if type(save_enabled) is not bool or type(recall_enabled) is not bool:
            raise ValueError("Memory owner policy 开关必须是 bool")
        normalized_rules = standing_rules.strip()
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Memory owner policy expected_revision 无效")
        if len(normalized_rules) > MAX_STANDING_RULES_CHARS:
            raise ValueError(f"standing_rules 最多 {MAX_STANDING_RULES_CHARS} 个字符")
        now = _iso()

        def operation(connection: sqlite3.Connection) -> OwnerMemoryPolicy:
            row = connection.execute(
                "SELECT revision FROM cowork_memory_owner_policy WHERE singleton_id = 1"
            ).fetchone()
            current_revision = 0 if row is None else int(row["revision"])
            if current_revision != expected_revision:
                raise MemoryPolicyConflictError()
            next_revision = current_revision + 1
            connection.execute(
                """INSERT INTO cowork_memory_owner_policy(
                       singleton_id, save_enabled, recall_enabled, standing_rules,
                       revision, updated_at
                   ) VALUES (1, ?, ?, ?, ?, ?)
                   ON CONFLICT(singleton_id) DO UPDATE SET
                       save_enabled = excluded.save_enabled,
                       recall_enabled = excluded.recall_enabled,
                       standing_rules = excluded.standing_rules,
                       revision = excluded.revision,
                       updated_at = excluded.updated_at""",
                (
                    int(save_enabled),
                    int(recall_enabled),
                    normalized_rules,
                    next_revision,
                    now,
                ),
            )
            return OwnerMemoryPolicy(
                save_enabled=save_enabled,
                recall_enabled=recall_enabled,
                standing_rules=normalized_rules,
                revision=next_revision,
            )

        return await self._write(operation)

    async def get_conversation_memory_policy(
        self, *, conversation_id: UUID
    ) -> ConversationMemoryPolicy:
        def operation(connection: sqlite3.Connection) -> ConversationMemoryPolicy:
            if (
                connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (str(conversation_id),)
                ).fetchone()
                is None
            ):
                raise ConversationNotFoundError(str(conversation_id))
            row = connection.execute(
                """SELECT save_mode, recall_mode, revision
                   FROM cowork_memory_conversation_policies WHERE conversation_id = ?""",
                (str(conversation_id),),
            ).fetchone()
            if row is None:
                return ConversationMemoryPolicy(conversation_id=conversation_id)
            return ConversationMemoryPolicy(
                conversation_id=conversation_id,
                save_mode=cast(MemoryPolicyMode, row["save_mode"]),
                recall_mode=cast(MemoryPolicyMode, row["recall_mode"]),
                revision=int(row["revision"]),
            )

        return await self._read(operation)

    async def upsert_conversation_memory_policy(
        self,
        *,
        conversation_id: UUID,
        save_mode: MemoryPolicyMode,
        recall_mode: MemoryPolicyMode,
        expected_revision: int,
    ) -> ConversationMemoryPolicy:
        if save_mode not in {"inherit", "on", "off"}:
            raise ValueError("conversation memory save_mode 无效")
        if recall_mode not in {"inherit", "on", "off"}:
            raise ValueError("conversation memory recall_mode 无效")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("conversation memory expected_revision 无效")
        now = _iso()

        def operation(connection: sqlite3.Connection) -> ConversationMemoryPolicy:
            if (
                connection.execute(
                    "SELECT 1 FROM conversations WHERE id = ?", (str(conversation_id),)
                ).fetchone()
                is None
            ):
                raise ConversationNotFoundError(str(conversation_id))
            row = connection.execute(
                """SELECT revision FROM cowork_memory_conversation_policies
                   WHERE conversation_id = ?""",
                (str(conversation_id),),
            ).fetchone()
            current_revision = 0 if row is None else int(row["revision"])
            if current_revision != expected_revision:
                raise MemoryPolicyConflictError()
            next_revision = current_revision + 1
            connection.execute(
                """INSERT INTO cowork_memory_conversation_policies(
                       conversation_id, save_mode, recall_mode, revision, updated_at
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                       save_mode = excluded.save_mode,
                       recall_mode = excluded.recall_mode,
                       revision = excluded.revision,
                       updated_at = excluded.updated_at""",
                (str(conversation_id), save_mode, recall_mode, next_revision, now),
            )
            return ConversationMemoryPolicy(
                conversation_id=conversation_id,
                save_mode=save_mode,
                recall_mode=recall_mode,
                revision=next_revision,
            )

        return await self._write(operation)

    # ---- 记忆抽取作业 ----------------------------------------------------------

    async def schedule_memory_extraction(
        self,
        *,
        run_id: UUID,
        conversation_id: UUID | None,
        source_message_id: UUID | None,
        content: str,
        source_created_at: datetime,
    ) -> Any | None:
        """按 run 幂等入队；已存在时返回既有作业，不重置重试计数。"""

        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> Any | None:
            connection.execute(
                """INSERT INTO memory_extraction_jobs(
                       id, run_id, conversation_id, source_message_id, content,
                       source_created_at, available_at, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id) DO NOTHING""",
                (
                    str(uuid7()),
                    str(run_id),
                    None if conversation_id is None else str(conversation_id),
                    None if source_message_id is None else str(source_message_id),
                    content,
                    _iso(source_created_at),
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM memory_extraction_jobs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            return None if row is None else self._memory_job_record(row)

        return await self._write(operation)

    async def claim_memory_job(
        self, *, job_id: UUID, worker_id: str, lease_s: int, max_attempts: int
    ) -> Any | None:
        now = _iso()
        lease_until = _iso(_now() + timedelta(seconds=lease_s))

        def operation(connection: sqlite3.Connection) -> Any | None:
            changed = connection.execute(
                """UPDATE memory_extraction_jobs
                   SET status = 'running', worker_id = ?, lease_until = ?,
                       attempts = attempts + 1, error = NULL, result_json = NULL,
                       finished_at = NULL, updated_at = ?
                   WHERE id = ?
                     AND attempts < ?
                     AND available_at <= ?
                     AND (status = 'queued' OR (status = 'running' AND lease_until < ?))""",
                (worker_id, lease_until, now, str(job_id), max_attempts, now, now),
            ).rowcount
            if not changed:
                return None
            row = connection.execute(
                "SELECT * FROM memory_extraction_jobs WHERE id = ?", (str(job_id),)
            ).fetchone()
            return self._memory_job_record(row)

        return await self._write(operation)

    async def get_memory_job(self, *, job_id: UUID) -> Any | None:
        return await self._read(
            lambda connection: (
                None
                if (
                    row := connection.execute(
                        "SELECT * FROM memory_extraction_jobs WHERE id = ?",
                        (str(job_id),),
                    ).fetchone()
                )
                is None
                else self._memory_job_record(row)
            )
        )

    async def complete_memory_job(
        self, *, job_id: UUID, worker_id: str, result: dict[str, Any]
    ) -> bool:
        now = _iso()
        normalized_result = normalize_memory_job_result(result)
        result_json = json.dumps(
            normalized_result,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        def operation(connection: sqlite3.Connection) -> bool:
            return bool(
                connection.execute(
                    """UPDATE memory_extraction_jobs
                       SET status = 'done', worker_id = NULL, lease_until = NULL,
                           content = '', finished_at = ?, error = NULL,
                           result_json = ?, updated_at = ?
                       WHERE id = ? AND status = 'running' AND worker_id = ?""",
                    (now, result_json, now, str(job_id), worker_id),
                ).rowcount
            )

        return await self._write(operation)

    async def retry_or_fail_memory_job(
        self, *, job_id: UUID, worker_id: str, error: str, max_attempts: int, retry_delay_s: int
    ) -> str | None:
        now = _now()

        def operation(connection: sqlite3.Connection) -> str | None:
            row = connection.execute(
                "SELECT attempts FROM memory_extraction_jobs WHERE id = ? AND worker_id = ?",
                (str(job_id), worker_id),
            ).fetchone()
            if row is None:
                return None
            exhausted = int(row["attempts"]) >= max_attempts
            stored_error = (
                "memory_extraction_failed" if exhausted else _memory_job_retry_error(error)
            )
            connection.execute(
                """UPDATE memory_extraction_jobs
                   SET status = ?, worker_id = NULL, lease_until = NULL, error = ?,
                       content = CASE WHEN ? THEN '' ELSE content END,
                       result_json = NULL, available_at = ?, finished_at = ?, updated_at = ?
                   WHERE id = ? AND worker_id = ?""",
                (
                    "failed" if exhausted else "queued",
                    stored_error,
                    int(exhausted),
                    _iso(now if exhausted else now + timedelta(seconds=retry_delay_s)),
                    _iso(now) if exhausted else None,
                    _iso(now),
                    str(job_id),
                    worker_id,
                ),
            )
            return "failed" if exhausted else "queued"

        return await self._write(operation)

    async def list_dispatchable_memory_jobs(
        self, *, max_attempts: int, limit: int = 100
    ) -> list[tuple[UUID, int]]:
        """扫出可派发作业，并把重试耗尽的收敛成 failed。

        没有这一步，attempts 打满的作业会永远留在 running 里既不跑也不失败——
        对着一个"看起来还在进行"的作业没人会去查。
        """

        now = _iso()

        def operation(connection: sqlite3.Connection) -> list[tuple[UUID, int]]:
            connection.execute(
                """UPDATE memory_extraction_jobs
                   SET status = 'failed', worker_id = NULL, lease_until = NULL,
                       content = '', error = 'memory_extraction_failed',
                       result_json = NULL, finished_at = ?, updated_at = ?
                   WHERE attempts >= ? AND status IN ('queued','running')
                     AND (lease_until IS NULL OR lease_until < ?)""",
                (now, now, max_attempts, now),
            )
            rows = connection.execute(
                """SELECT id, attempts FROM memory_extraction_jobs
                   WHERE attempts < ? AND available_at <= ?
                     AND (status = 'queued' OR (status = 'running' AND lease_until < ?))
                   ORDER BY available_at, id LIMIT ?""",
                (max_attempts, now, now, limit),
            ).fetchall()
            return [(UUID(row["id"]), int(row["attempts"])) for row in rows]

        return await self._write(operation)

    @staticmethod
    def _memory_job_record(row: sqlite3.Row) -> Any:
        result = (
            None
            if row["result_json"] is None
            else normalize_memory_job_result(json.loads(str(row["result_json"])))
        )
        return MemoryExtractionJob(
            id=UUID(row["id"]),
            run_id=UUID(row["run_id"]),
            conversation_id=(
                None if row["conversation_id"] is None else UUID(row["conversation_id"])
            ),
            source_message_id=(
                None if row["source_message_id"] is None else UUID(row["source_message_id"])
            ),
            content=str(row["content"]),
            source_created_at=cast(datetime, _datetime(row["source_created_at"])),
            status=row["status"],
            attempts=int(row["attempts"]),
            error=row["error"],
            created_at=cast(datetime, _datetime(row["created_at"])),
            result=result,
        )

    @staticmethod
    def _memory_record(row: sqlite3.Row) -> Any:
        return CoworkMemoryRecord(
            id=UUID(row["id"]),
            scope=row["scope"],
            conversation_id=(
                None if row["conversation_id"] is None else UUID(row["conversation_id"])
            ),
            workspace_path=row["workspace_path"],
            key=row["key"],
            content=str(row["content"]),
            source=row["source"],
            created_at=cast(datetime, _datetime(row["created_at"])),
            updated_at=cast(datetime, _datetime(row["updated_at"])),
            forgotten_at=_datetime(row["forgotten_at"]),
            category=row["category"],
            confidence=float(row["confidence"]),
            pinned=bool(row["pinned"]),
            valid_from=_datetime(row["valid_from"]) or _datetime(row["created_at"]),
            invalid_at=_datetime(row["invalid_at"]),
            superseded_by=(None if row["superseded_by"] is None else UUID(row["superseded_by"])),
            access_count=int(row["access_count"]),
            last_used_at=_datetime(row["last_used_at"]),
            source_message_id=(
                None if row["source_message_id"] is None else UUID(row["source_message_id"])
            ),
            run_id=None if row["run_id"] is None else UUID(row["run_id"]),
        )

    @staticmethod
    def _inbox_record(row: sqlite3.Row, record_type: Any) -> Any:
        return record_type(
            id=UUID(row["id"]),
            run_id=UUID(row["run_id"]),
            conversation_id=UUID(row["conversation_id"]),
            kind=row["kind"],
            status=row["status"],
            resume_token=UUID(row["resume_token"]),
            tool_call_id=str(row["tool_call_id"]),
            plan_step_id=UUID(row["plan_step_id"]),
            request=json.loads(row["request"]),
            response=None if row["response"] is None else json.loads(row["response"]),
            created_at=cast(datetime, _datetime(row["created_at"])),
            responded_at=_datetime(row["responded_at"]),
            unattended=bool(row["unattended"]),
        )

    @staticmethod
    def _team_record(row: sqlite3.Row) -> TeamRecord:
        raw_scope = json.loads(str(row["write_delegation_scope"]))
        raw_receipt = (
            None
            if row["write_delegation_receipt"] is None
            else json.loads(str(row["write_delegation_receipt"]))
        )
        if not isinstance(raw_scope, list):
            raise ValueError("Team write_delegation_scope 必须是 JSON array")
        if raw_receipt is not None and not isinstance(raw_receipt, dict):
            raise ValueError("Team write_delegation_receipt 必须是 JSON object")
        return TeamRecord(
            id=UUID(row["id"]),
            lead_conversation_id=UUID(row["lead_conversation_id"]),
            proposal_call_id=str(row["proposal_call_id"]),
            status=row["status"],
            note=str(row["note"]),
            write_delegation_scope=[
                {str(key): str(value) for key, value in item.items()}
                for item in raw_scope
                if isinstance(item, dict)
            ],
            write_delegation_receipt=(
                None if raw_receipt is None else cast("dict[str, Any]", raw_receipt)
            ),
            pause_reason=(None if row["pause_reason"] is None else str(row["pause_reason"])),
            budget_limits={
                "model_calls": int(row["budget_max_model_calls"]),
                "tool_calls": int(row["budget_max_tool_calls"]),
                "wall_ms": int(row["budget_max_wall_ms"]),
                "assignments": int(row["budget_max_assignments"]),
            },
            budget_usage={
                "model_calls": int(row["budget_used_model_calls"]),
                "tool_calls": int(row["budget_used_tool_calls"]),
                "wall_ms": int(row["budget_used_wall_ms"]),
                "assignments": int(row["budget_used_assignments"]),
                "reserved_model_calls": int(row["budget_reserved_model_calls"]),
                "reserved_tool_calls": int(row["budget_reserved_tool_calls"]),
                "reserved_wall_ms": int(row["budget_reserved_wall_ms"]),
            },
            created_at=cast(datetime, _datetime(row["created_at"])),
            updated_at=cast(datetime, _datetime(row["updated_at"])),
        )

    @staticmethod
    def _team_budget_reservation_record(row: sqlite3.Row) -> TeamBudgetReservationRecord:
        return TeamBudgetReservationRecord(
            id=UUID(str(row["id"])),
            team_id=UUID(str(row["team_id"])),
            task_id=UUID(str(row["task_id"])),
            assignment_call_id=str(row["assignment_call_id"]),
            status=row["status"],
            reserved={
                "model_calls": int(row["reserved_model_calls"]),
                "tool_calls": int(row["reserved_tool_calls"]),
                "wall_ms": int(row["reserved_wall_ms"]),
            },
            used={
                "model_calls": int(row["used_model_calls"]),
                "tool_calls": int(row["used_tool_calls"]),
                "wall_ms": int(row["used_wall_ms"]),
            },
            created_at=cast(datetime, _datetime(row["created_at"])),
            updated_at=cast(datetime, _datetime(row["updated_at"])),
            settled_at=_datetime(row["settled_at"]),
        )

    @staticmethod
    def _team_wake_delivery_record(row: sqlite3.Row) -> TeamWakeDeliveryRecord:
        payload = json.loads(str(row["payload"]))
        if not isinstance(payload, dict):
            raise TeamEventIntegrityError("Team wake delivery payload 不是 object")
        return TeamWakeDeliveryRecord(
            id=UUID(str(row["id"])),
            team_id=UUID(str(row["team_id"])),
            event_id=UUID(str(row["event_id"])),
            event_sequence=int(row["event_sequence"]),
            event_hash=str(row["event_hash"]),
            event_type=str(row["event_type"]),
            target_kind=row["target_kind"],
            target_id=None if row["target_id"] is None else str(row["target_id"]),
            payload=cast("dict[str, Any]", payload),
            status=row["status"],
            attempt_count=int(row["attempt_count"]),
            claim_owner=None if row["claim_owner"] is None else str(row["claim_owner"]),
            claim_until=_datetime(row["claim_until"]),
            validation_outcome=(
                None if row["validation_outcome"] is None else row["validation_outcome"]
            ),
            validated_at=_datetime(row["validated_at"]),
            delivery_receipt=(
                None if row["delivery_receipt"] is None else str(row["delivery_receipt"])
            ),
            last_error=None if row["last_error"] is None else str(row["last_error"]),
            created_at=cast(datetime, _datetime(row["created_at"])),
            updated_at=cast(datetime, _datetime(row["updated_at"])),
            delivered_at=_datetime(row["delivered_at"]),
        )

    @staticmethod
    def _team_worker_tool_attempt_record(row: sqlite3.Row) -> TeamWorkerToolAttemptRecord:
        raw_result = None if row["result"] is None else json.loads(str(row["result"]))
        raw_receipt = (
            None
            if row["authorization_receipt"] is None
            else json.loads(str(row["authorization_receipt"]))
        )
        if raw_result is not None and not isinstance(raw_result, dict):
            raise ValueError("Team tool attempt result 必须是 JSON object")
        if raw_receipt is not None and not isinstance(raw_receipt, dict):
            raise ValueError("Team tool attempt authorization receipt 必须是 JSON object")
        return TeamWorkerToolAttemptRecord(
            id=UUID(str(row["id"])),
            team_id=UUID(str(row["team_id"])),
            session_id=UUID(str(row["session_id"])),
            task_id=UUID(str(row["task_id"])),
            tool_call_id=str(row["tool_call_id"]),
            tool_name=str(row["tool_name"]),
            effect=str(row["effect"]),
            retry_safe=bool(row["retry_safe"]),
            status=row["status"],
            arguments_sha256=str(row["arguments_sha256"]),
            attempt_count=int(row["attempt_count"]),
            result=(None if raw_result is None else cast("dict[str, Any]", raw_result)),
            effect_ref=None if row["effect_ref"] is None else str(row["effect_ref"]),
            authorization_receipt=(
                None if raw_receipt is None else cast("dict[str, Any]", raw_receipt)
            ),
            started_at=cast(datetime, _datetime(row["started_at"])),
            finished_at=_datetime(row["finished_at"]),
            updated_at=cast(datetime, _datetime(row["updated_at"])),
        )

    @staticmethod
    def _team_worker_record(row: sqlite3.Row) -> TeamWorkerRecord:
        return TeamWorkerRecord(
            id=UUID(row["id"]),
            team_id=UUID(row["team_id"]),
            name=str(row["name"]),
            role=str(row["role"]),
            reason=str(row["reason"]),
            session_id=UUID(row["session_id"]),
            created_at=cast(datetime, _datetime(row["created_at"])),
        )

    @staticmethod
    def _team_worker_session_record(row: sqlite3.Row) -> TeamWorkerSessionRecord:
        state = json.loads(str(row["state"]))
        if not isinstance(state, dict):  # pragma: no cover - 写入端固定为 JSON object
            raise ValueError("Worker Session state 必须是 JSON object")
        return TeamWorkerSessionRecord(
            id=UUID(row["id"]),
            team_id=UUID(row["team_id"]),
            worker_id=UUID(row["worker_id"]),
            status=row["status"],
            active_task_id=(None if row["active_task_id"] is None else UUID(row["active_task_id"])),
            state=cast("dict[str, Any]", state),
            created_at=cast(datetime, _datetime(row["created_at"])),
            updated_at=cast(datetime, _datetime(row["updated_at"])),
        )

    @staticmethod
    def _board_task_record(row: sqlite3.Row) -> BoardTaskRecord:
        raw_scope = json.loads(str(row["resource_scope"]))
        raw_receipt = (
            None if row["scope_receipt"] is None else json.loads(str(row["scope_receipt"]))
        )
        if not isinstance(raw_scope, list):  # pragma: no cover - 写入端固定为 JSON array
            raise ValueError("Board task resource_scope 必须是 JSON array")
        if raw_receipt is not None and not isinstance(raw_receipt, dict):
            raise ValueError("Board task scope_receipt 必须是 JSON object")
        return BoardTaskRecord(
            id=UUID(row["id"]),
            team_id=UUID(row["team_id"]),
            title=str(row["title"]),
            description=str(row["description"]),
            acceptance_criteria=str(row["acceptance_criteria"]),
            resource_scope=[
                {str(key): str(value) for key, value in item.items()}
                for item in raw_scope
                if isinstance(item, dict)
            ],
            scope_receipt=(None if raw_receipt is None else cast("dict[str, Any]", raw_receipt)),
            status=row["status"],
            assignee_worker_id=(
                None if row["assignee_worker_id"] is None else UUID(row["assignee_worker_id"])
            ),
            assignment_call_id=(
                None if row["assignment_call_id"] is None else str(row["assignment_call_id"])
            ),
            attempt_count=int(row["attempt_count"]),
            completion_kind=row["completion_kind"],
            worker_report=None if row["worker_report"] is None else str(row["worker_report"]),
            review_comment=(None if row["review_comment"] is None else str(row["review_comment"])),
            last_rejection_comment=(
                None
                if row["last_rejection_comment"] is None
                else str(row["last_rejection_comment"])
            ),
            last_error=None if row["last_error"] is None else str(row["last_error"]),
            created_at=cast(datetime, _datetime(row["created_at"])),
            updated_at=cast(datetime, _datetime(row["updated_at"])),
        )

    @staticmethod
    def _schedule_record(row: sqlite3.Row, record_type: Any) -> Any:
        return record_type(
            id=UUID(row["id"]),
            conversation_id=UUID(row["conversation_id"]),
            title=str(row["title"]),
            goal=str(row["goal"]),
            schedule_kind=row["schedule_kind"],
            cron_expression=row["cron_expression"],
            run_at=_datetime(row["run_at"]),
            timezone=str(row["timezone"]),
            enabled=bool(row["enabled"]),
            next_run_at=_datetime(row["next_run_at"]),
            last_run_at=_datetime(row["last_run_at"]),
            last_run_id=None if row["last_run_id"] is None else UUID(row["last_run_id"]),
            run_count=int(row["run_count"]),
            skipped_count=int(row["skipped_count"]),
            created_at=cast(datetime, _datetime(row["created_at"])),
            updated_at=cast(datetime, _datetime(row["updated_at"])),
        )

    @staticmethod
    def _run_record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=UUID(row["id"]),
            conversation_id=UUID(row["conversation_id"]),
            goal=str(row["goal"]),
            status=str(row["status"]),
            worker_id=row["worker_id"],
            lease_until=_datetime(row["lease_until"]),
            cancel_requested_at=_datetime(row["cancel_requested_at"]),
            budget_tokens=int(row["budget_tokens"]),
            budget_calls=int(row["budget_calls"]),
            budget_wall_ms=int(row["budget_wall_ms"]),
            used_tokens=int(row["used_tokens"]),
            used_calls=int(row["used_calls"]),
            next_seq=int(row["next_seq"]),
            error=row["error"],
            schedule_id=None if row["schedule_id"] is None else UUID(row["schedule_id"]),
            unattended=bool(row["unattended"]),
            run_trigger=row["run_trigger"],
            workflow_type=row["workflow_type"],
            answer_mode=row["answer_mode"],
            retrieval_top_k=int(row["retrieval_top_k"]),
        )
