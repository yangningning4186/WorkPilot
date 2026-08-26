"""SQLite Cowork 控制面实现。

每个操作使用独立连接和短事务；模型、浏览器、Shell 等慢操作绝不持有 SQLite 锁。
WAL 允许 SSE/客户端读取与 worker 写入并行，busy_timeout 处理极短的写竞争。
"""

from __future__ import annotations

import asyncio
import json
import os
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
    canonical_json,
    invocation_identity,
)
from app.cowork_contracts import (
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
    ConversationNotFoundError,
    CoworkAttachmentError,
    CoworkAttachmentRecord,
    CoworkMemoryRecord,
    InboxBindingRecord,
    InboxRecord,
    MemoryExtractionJob,
    MemoryNotFoundError,
    PathAuthorization,
    PinnedMemoryError,
    ReadingAnnotationRecord,
    ScheduleRecord,
    ScheduleView,
    SessionRootNotFoundError,
    SessionRootRecord,
    SteeringRecord,
    TeamRecord,
    TeamWorkerRecord,
    TeamWorkerSessionRecord,
    ThreadSessionRecord,
    UnattendedInboxRecord,
    UnroutedRecord,
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
from app.cowork_store.base import StoredCheckpoint

T = TypeVar("T")

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

CREATE TABLE IF NOT EXISTS cowork_steering_messages (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    consumed_at TEXT
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
-- 记忆抽取作业。和记忆写在同一个库里，所以"提炼出一条记忆"与"把作业标记完成"
-- 可以在同一个事务里，不会出现记了却没结算、或结算了没记的半截状态。
-- 来源快照（消息内容、会话、时间）随作业一起存：claim 时不必回查会话，
-- 也就没有了"作业还在、来源消息已被删除"这一类失败。
CREATE TABLE IF NOT EXISTS memory_extraction_jobs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    conversation_id TEXT,
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
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_local_memory_jobs_dispatch
ON memory_extraction_jobs(available_at, id) WHERE status IN ('queued','running');

PRAGMA user_version = 14;
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
            if database_version > 14:
                raise RuntimeError(
                    f"cowork.db schema v{database_version} 高于当前应用支持的 v14，拒绝降级打开"
                )
            connection.executescript(_SCHEMA)
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
            grant_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(capability_grants)").fetchall()
            }
            if "resource_scope" not in grant_columns:
                connection.execute("ALTER TABLE capability_grants ADD COLUMN resource_scope TEXT")
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
            board_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(cowork_board_tasks)").fetchall()
            }
            for column, ddl in (
                ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
                ("completion_kind", "TEXT NOT NULL DEFAULT 'pending'"),
                ("last_rejection_comment", "TEXT"),
                ("last_error", "TEXT"),
            ):
                if column not in board_columns:
                    connection.execute(f"ALTER TABLE cowork_board_tasks ADD COLUMN {column} {ddl}")
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
            connection.execute("PRAGMA user_version = 14")
        finally:
            connection.close()
        try:
            self.path.chmod(0o600)
            self.path.parent.chmod(0o700)
        except PermissionError:  # pragma: no cover - 不支持 chmod 的文件系统
            pass

    async def close(self) -> None:
        return None

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
            "agent_runs",
            "run_events",
            "agent_plan_steps",
            "agent_attempts",
            "agent_checkpoints",
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
            "agent_runs",
            "run_events",
            "agent_plan_steps",
            "agent_attempts",
            "agent_checkpoints",
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
                f"""SELECT conversations.*,
                           (SELECT COUNT(*) FROM conversation_message_index AS messages
                            WHERE messages.conversation_id = conversations.id
                              AND messages.role IN ('user', 'assistant')) AS message_count,
                           (SELECT messages.content_preview
                            FROM conversation_message_index AS messages
                            WHERE messages.conversation_id = conversations.id
                              AND messages.role IN ('user', 'assistant')
                              AND messages.content_preview IS NOT NULL
                              AND messages.content_preview <> ''
                            ORDER BY messages.seq DESC LIMIT 1) AS latest_message,
                           (SELECT messages.created_at
                            FROM conversation_message_index AS messages
                            WHERE messages.conversation_id = conversations.id
                              AND messages.role IN ('user', 'assistant')
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
        return await self._write(
            lambda connection: (
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
        )

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
    ) -> RunRecord:
        if not goal.strip():
            raise ValueError("run 目标不能为空")
        if not 1 <= retrieval_top_k <= 20:
            raise ValueError("retrieval_top_k 必须位于 1 到 20")
        run_id = uuid7()
        timestamp = _iso()
        initial_status = "initializing" if initializing else "queued"

        def operation(connection: sqlite3.Connection) -> RunRecord:
            connection.execute(
                """
                INSERT INTO agent_runs(
                    id, conversation_id, goal, status, budget_tokens, budget_calls,
                    budget_wall_ms, answer_mode, workflow_type, schedule_id, unattended, run_trigger,
                    retrieval_top_k,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    async def initialize_run(
        self,
        *,
        run_id: UUID,
        state: dict[str, Any],
        checkpoint_id: str,
        events: Sequence[tuple[str, dict[str, Any]]],
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
            connection.execute(
                """INSERT INTO agent_checkpoints(
                           run_id, checkpoint_id, parent_id, state, created_at
                       ) VALUES (?, ?, NULL, ?, ?)""",
                (str(run_id), checkpoint_id, canonical_json(state), _iso()),
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
        self, *, run_id: UUID, events: Sequence[tuple[str, dict[str, Any]]]
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
        events: Sequence[tuple[str, dict[str, Any]]],
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
        encoded = [
            (
                str(run_id),
                first_seq + offset,
                event_type,
                canonical_json(payload),
                created_at,
            )
            for offset, (event_type, payload) in enumerate(events)
        ]
        connection.executemany(
            "INSERT INTO run_events(run_id, seq, type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            encoded,
        )
        output = [
            RunEvent(run_id, first_seq + offset, event_type, payload, timestamp)
            for offset, (event_type, payload) in enumerate(events)
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
                    type=str(row["type"]),
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
            connection.execute(
                """
                INSERT INTO agent_checkpoints(run_id, checkpoint_id, parent_id, state, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(run_id),
                    checkpoint.checkpoint_id,
                    parent_id,
                    canonical_json(state),
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
        events: Sequence[tuple[str, dict[str, Any]]],
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
                "SELECT status, worker_id FROM agent_runs WHERE id = ?", (str(run_id),)
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
            connection.execute(
                """INSERT INTO agent_checkpoints(
                           run_id, checkpoint_id, parent_id, state, created_at
                       ) VALUES (?, ?, ?, ?, ?)""",
                (
                    str(run_id),
                    checkpoint_id,
                    parent_id,
                    canonical_json(state),
                    _iso(),
                ),
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
            return StoredCheckpoint(
                run_id=UUID(row["run_id"]),
                checkpoint_id=str(row["checkpoint_id"]),
                parent_id=row["parent_id"],
                state=json.loads(row["state"]),
            )

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
            return StoredCheckpoint(
                run_id=UUID(row["run_id"]),
                checkpoint_id=str(row["checkpoint_id"]),
                parent_id=row["parent_id"],
                state=json.loads(row["state"]),
            )

        return await self._read(operation)

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

        def operation(connection: sqlite3.Connection) -> InvocationLease:
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
            expired = status == "in_flight" and str(existing["lease_until"] or "") < _iso(now)
            if status == "failed" or expired:
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

        return await self._write(operation)

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
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                UPDATE tool_invocations
                SET status = 'failed', result = ?, lease_owner = NULL, lease_until = NULL,
                    updated_at = ?
                WHERE idempotency_key = ? AND status = 'in_flight' AND lease_owner = ?
                """,
                (canonical_json({"error": error}), _iso(), key, worker_id),
            )

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

    async def set_workspace_trust(self, *, canonical_path: str, trusted: bool) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            if trusted:
                connection.execute(
                    """INSERT INTO cowork_workspace_trust(canonical_path, trusted_at, revoked_at)
                       VALUES (?, ?, NULL)
                       ON CONFLICT(canonical_path) DO UPDATE
                       SET trusted_at = excluded.trusted_at, revoked_at = NULL""",
                    (canonical_path, _iso()),
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

    async def is_workspace_trusted(self, *, canonical_path: str) -> bool:
        return await self._read(
            lambda connection: (
                connection.execute(
                    """SELECT 1 FROM cowork_workspace_trust
                   WHERE canonical_path = ? AND revoked_at IS NULL""",
                    (canonical_path,),
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

    async def enqueue_steering(self, *, run_id: UUID, conversation_id: UUID, content: str) -> Any:
        normalized = content.strip()
        if not 1 <= len(normalized) <= 4000:
            raise ValueError("steering 内容长度必须位于 1 到 4000")
        item_id = uuid7()
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> Any:
            connection.execute(
                """INSERT INTO cowork_steering_messages(
                       id, run_id, conversation_id, content, status, created_at
                   ) VALUES (?, ?, ?, ?, 'pending', ?)""",
                (str(item_id), str(run_id), str(conversation_id), normalized, timestamp),
            )
            return SteeringRecord(
                item_id,
                run_id,
                conversation_id,
                normalized,
                "pending",
                cast(datetime, _datetime(timestamp)),
                None,
            )

        return await self._write(operation)

    async def consume_pending_steering(self, *, run_id: UUID) -> list[Any]:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> list[Any]:
            rows = connection.execute(
                """SELECT * FROM cowork_steering_messages
                   WHERE run_id = ? AND status = 'pending' ORDER BY created_at, id""",
                (str(run_id),),
            ).fetchall()
            if rows:
                connection.executemany(
                    """UPDATE cowork_steering_messages
                       SET status = 'consumed', consumed_at = ? WHERE id = ? AND status = 'pending'""",
                    [(timestamp, row["id"]) for row in rows],
                )
            return [
                SteeringRecord(
                    UUID(row["id"]),
                    UUID(row["run_id"]),
                    UUID(row["conversation_id"]),
                    str(row["content"]),
                    "consumed",
                    cast(datetime, _datetime(row["created_at"])),
                    cast(datetime, _datetime(timestamp)),
                )
                for row in rows
            ]

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
                """UPDATE cowork_steering_messages SET status = 'cancelled'
                   WHERE run_id = ? AND status = 'pending'""",
                (str(run_id),),
            )

        await self._write(operation)

    # ---- Agent Teams / Board -----------------------------------------------
    async def create_team(
        self,
        *,
        lead_conversation_id: UUID,
        proposal_call_id: str,
        note: str,
        members: Sequence[dict[str, Any]],
    ) -> tuple[TeamRecord, list[TeamWorkerRecord]]:
        """审批通过后原子创建 roster 与零 token 的空闲 Worker Session。"""

        timestamp = _iso()

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
                       created_at, updated_at
                   ) VALUES (?, ?, ?, 'active', ?, ?, ?)""",
                (
                    str(team_id),
                    str(lead_conversation_id),
                    proposal_call_id,
                    note,
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

    async def create_board_task(
        self,
        *,
        lead_conversation_id: UUID,
        title: str,
        description: str,
        acceptance_criteria: str,
        resource_scope: Sequence[dict[str, str]],
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
                       resource_scope, status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                (
                    str(task_id),
                    str(team["id"]),
                    title,
                    description,
                    acceptance_criteria,
                    canonical_json(resource_scope),
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM cowork_board_tasks WHERE id = ?", (str(task_id),)
            ).fetchone()
            assert row is not None
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
    ) -> tuple[BoardTaskRecord, TeamWorkerRecord, TeamWorkerSessionRecord]:
        timestamp = _iso()

        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[BoardTaskRecord, TeamWorkerRecord, TeamWorkerSessionRecord]:
            row = connection.execute(
                """SELECT tasks.*, teams.lead_conversation_id
                   FROM cowork_board_tasks AS tasks
                   JOIN cowork_teams AS teams ON teams.id = tasks.team_id
                   WHERE tasks.id = ?""",
                (str(task_id),),
            ).fetchone()
            if row is None or str(row["lead_conversation_id"]) != str(lead_conversation_id):
                raise ValueError("Board task 不存在或不属于当前 Lead 会话")
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
            return (
                self._board_task_record(task_row),
                self._team_worker_record(worker_row),
                self._team_worker_session_record(current_session),
            )

        return await self._write(operation)

    async def save_team_worker_session(
        self,
        *,
        session_id: UUID,
        task_id: UUID,
        state: dict[str, Any],
    ) -> TeamWorkerSessionRecord:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> TeamWorkerSessionRecord:
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
            return self._team_worker_session_record(row)

        return await self._write(operation)

    async def complete_board_task(
        self,
        *,
        session_id: UUID,
        task_id: UUID,
        state: dict[str, Any],
        worker_report: str,
    ) -> BoardTaskRecord:
        return await self._finish_worker_task(
            session_id=session_id,
            task_id=task_id,
            state=state,
            task_status="review",
            worker_report=worker_report,
            last_error=None,
            session_status="idle",
        )

    async def fail_board_task(
        self,
        *,
        session_id: UUID,
        task_id: UUID,
        state: dict[str, Any],
        error: str,
    ) -> BoardTaskRecord:
        return await self._finish_worker_task(
            session_id=session_id,
            task_id=task_id,
            state=state,
            task_status="blocked",
            worker_report=None,
            last_error=error,
            session_status="idle",
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
            return self._board_task_record(row)

        return await self._write(operation)

    async def review_board_task(
        self,
        *,
        lead_conversation_id: UUID,
        task_id: UUID,
        accepted: bool,
        feedback: str,
    ) -> BoardTaskRecord:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> BoardTaskRecord:
            row = connection.execute(
                """SELECT tasks.* FROM cowork_board_tasks AS tasks
                   JOIN cowork_teams AS teams ON teams.id = tasks.team_id
                   WHERE tasks.id = ? AND teams.lead_conversation_id = ?""",
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
            return self._board_task_record(updated)

        return await self._write(operation)

    async def resolve_board_task(
        self,
        *,
        lead_conversation_id: UUID,
        task_id: UUID,
        resolution: Literal["accept_partial", "cancel"],
        reason: str,
    ) -> BoardTaskRecord:
        """显式收束无法继续执行的任务；不会伪造一次 Worker review。"""

        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> BoardTaskRecord:
            row = connection.execute(
                """SELECT tasks.* FROM cowork_board_tasks AS tasks
                   JOIN cowork_teams AS teams ON teams.id = tasks.team_id
                   WHERE tasks.id = ? AND teams.lead_conversation_id = ?""",
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
            return (
                connection.execute(
                    f"""UPDATE agent_runs SET status = ?, error = ?,
                               used_tokens = used_tokens + ?, used_calls = used_calls + ?,
                               finished_at = ?, lease_until = NULL, updated_at = ?
                        WHERE id = ? AND status <> ? {owner_sql}""",
                    parameters,
                ).rowcount
                == 1
            )

        return await self._write(operation)

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
                                 WHERE checkpoints.run_id = runs.id) AS has_checkpoint
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
                if bool(row["has_checkpoint"]) and recovery < max_recovery:
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
    ) -> tuple[Any, Any]:
        memory_id = uuid7()
        timestamp = _iso()
        effective_from = _iso(valid_from)

        def operation(connection: sqlite3.Connection) -> tuple[Any, Any]:
            previous: Any = None
            if key is not None:
                row = connection.execute(
                    """SELECT * FROM cowork_memories
                       WHERE scope = ?
                         AND IFNULL(conversation_id, '') = IFNULL(?, '')
                         AND IFNULL(workspace_path, '') = IFNULL(?, '')
                         AND key = ? AND forgotten_at IS NULL""",
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
                    connection.execute(
                        """UPDATE cowork_memories
                           SET content = ?, source = ?, category = ?, confidence = ?,
                               valid_from = ?, source_message_id = ?, run_id = ?,
                               updated_at = ?
                           WHERE id = ?""",
                        (
                            content,
                            source,
                            category,
                            confidence,
                            effective_from,
                            None if source_message_id is None else str(source_message_id),
                            None if run_id is None else str(run_id),
                            timestamp,
                            str(previous.id),
                        ),
                    )
                    updated = connection.execute(
                        "SELECT * FROM cowork_memories WHERE id = ?", (str(previous.id),)
                    ).fetchone()
                    return self._memory_record(updated), previous
            connection.execute(
                """INSERT INTO cowork_memories(
                       id, scope, conversation_id, workspace_path, key, content, source,
                       created_at, updated_at, category, confidence, pinned, valid_from,
                       source_message_id, run_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    effective_from,
                    None if source_message_id is None else str(source_message_id),
                    None if run_id is None else str(run_id),
                ),
            )
            row = connection.execute(
                "SELECT * FROM cowork_memories WHERE id = ?", (str(memory_id),)
            ).fetchone()
            return self._memory_record(row), None

        return await self._write(operation)

    async def update_cowork_memory(
        self, *, memory_id: UUID, content: str | None, restore: bool
    ) -> tuple[Any, Any]:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> tuple[Any, Any]:
            row = connection.execute(
                "SELECT * FROM cowork_memories WHERE id = ?", (str(memory_id),)
            ).fetchone()
            if row is None:
                raise MemoryNotFoundError(str(memory_id))
            previous = self._memory_record(row)
            connection.execute(
                """UPDATE cowork_memories
                   SET content = COALESCE(?, content),
                       forgotten_at = CASE WHEN ? THEN NULL ELSE forgotten_at END,
                       updated_at = ?
                   WHERE id = ?""",
                (content, int(restore), timestamp, str(memory_id)),
            )
            updated = connection.execute(
                "SELECT * FROM cowork_memories WHERE id = ?", (str(memory_id),)
            ).fetchone()
            return self._memory_record(updated), previous

        return await self._write(operation)

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

    async def set_cowork_memory_pinned(self, *, memory_id: UUID, pinned: bool) -> Any | None:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> Any | None:
            changed = connection.execute(
                "UPDATE cowork_memories SET pinned = ?, updated_at = ? WHERE id = ?",
                (int(pinned), timestamp, str(memory_id)),
            ).rowcount
            if not changed:
                return None
            row = connection.execute(
                "SELECT * FROM cowork_memories WHERE id = ?", (str(memory_id),)
            ).fetchone()
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
                       attempts = attempts + 1, error = NULL, updated_at = ?
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

    async def complete_memory_job(self, *, job_id: UUID, worker_id: str) -> bool:
        now = _iso()

        def operation(connection: sqlite3.Connection) -> bool:
            return bool(
                connection.execute(
                    """UPDATE memory_extraction_jobs
                       SET status = 'done', worker_id = NULL, lease_until = NULL,
                           finished_at = ?, error = NULL, updated_at = ?
                       WHERE id = ? AND status = 'running' AND worker_id = ?""",
                    (now, now, str(job_id), worker_id),
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
            connection.execute(
                """UPDATE memory_extraction_jobs
                   SET status = ?, worker_id = NULL, lease_until = NULL, error = ?,
                       available_at = ?, finished_at = ?, updated_at = ?
                   WHERE id = ? AND worker_id = ?""",
                (
                    "failed" if exhausted else "queued",
                    error[:2_000],
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
                       finished_at = ?, updated_at = ?
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
        return TeamRecord(
            id=UUID(row["id"]),
            lead_conversation_id=UUID(row["lead_conversation_id"]),
            proposal_call_id=str(row["proposal_call_id"]),
            status=row["status"],
            note=str(row["note"]),
            created_at=cast(datetime, _datetime(row["created_at"])),
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
        if not isinstance(raw_scope, list):  # pragma: no cover - 写入端固定为 JSON array
            raise ValueError("Board task resource_scope 必须是 JSON array")
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
