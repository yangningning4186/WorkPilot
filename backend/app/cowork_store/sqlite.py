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
    ArtifactRecord,
    ArtifactRegistrationError,
    CapabilityDeniedError,
    CapabilityGrantRecord,
    ConversationBusyError,
    ConversationNotFoundError,
    CoworkAttachmentError,
    CoworkAttachmentRecord,
    CoworkMemoryRecord,
    InboxRecord,
    MemoryNotFoundError,
    PathAuthorization,
    ScheduleRecord,
    ScheduleView,
    SessionRootNotFoundError,
    SessionRootRecord,
    SteeringRecord,
    UnattendedInboxRecord,
)
from app.cowork_policy import (
    ALL_CAPABILITIES,
    GLOBAL_CAPABILITIES,
    PATH_CAPABILITIES,
    canonicalize_root,
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
    grant_source TEXT NOT NULL DEFAULT 'user',
    expires_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_local_active_grant
ON capability_grants(conversation_id, IFNULL(session_root_id, ''), capability)
WHERE revoked_at IS NULL;

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
    created_at TEXT NOT NULL,
    responded_at TEXT,
    UNIQUE (run_id, tool_call_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_local_pending_inbox
ON cowork_inbox_items(run_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS cowork_steering_messages (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    consumed_at TEXT
);

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
    CHECK (
        (scope = 'global' AND conversation_id IS NULL AND workspace_path IS NULL)
        OR (scope = 'workspace' AND conversation_id IS NULL AND workspace_path IS NOT NULL)
        OR (scope = 'conversation' AND conversation_id IS NOT NULL AND workspace_path IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS ix_local_cowork_memories_active
ON cowork_memories(scope, updated_at) WHERE forgotten_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_local_cowork_memories_key
ON cowork_memories(scope, IFNULL(conversation_id, ''), IFNULL(workspace_path, ''), key)
WHERE key IS NOT NULL AND forgotten_at IS NULL;

PRAGMA user_version = 5;
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
            connection.executescript(_SCHEMA)
            conversation_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(conversations)").fetchall()
            }
            if "archived_at" not in conversation_columns:
                connection.execute("ALTER TABLE conversations ADD COLUMN archived_at TEXT")
            run_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(agent_runs)").fetchall()
            }
            if "retrieval_top_k" not in run_columns:
                connection.execute(
                    "ALTER TABLE agent_runs ADD COLUMN retrieval_top_k INTEGER NOT NULL DEFAULT 5"
                )
            connection.execute("PRAGMA user_version = 5")
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
                       record_id, conversation_id, seq, role, status, run_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(record_id),
                    str(conversation_id),
                    seq,
                    role,
                    status,
                    None if run_id is None else str(run_id),
                    timestamp,
                ),
            )
            if role == "user":
                connection.execute(
                    """UPDATE conversations SET
                           title = CASE WHEN title IS NULL OR title = '新会话' THEN ? ELSE title END,
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

    async def update_message_status(self, *, record_id: UUID, status: str) -> None:
        await self._write(
            lambda connection: connection.execute(
                "UPDATE conversation_message_index SET status = ? WHERE record_id = ?",
                (status, str(record_id)),
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
                f"""SELECT * FROM conversations {where}
                    ORDER BY updated_at DESC, id DESC LIMIT ?""",
                tuple(parameters),
            ).fetchall()
            return [dict(row) for row in rows]

        return await self._read(operation)

    async def set_conversation_archived(
        self, *, conversation_id: UUID, archived: bool
    ) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            active = connection.execute(
                """SELECT 1 FROM agent_runs WHERE conversation_id = ?
                   AND status NOT IN ('done','failed','cancelled','budget_exceeded') LIMIT 1""",
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
    ) -> bool:
        return await self._write(
            lambda connection: (
                connection.execute(
                    """UPDATE conversations SET provider_profile_id = ?, model_override = ?,
                          unattended = ?, updated_at = ? WHERE id = ?""",
                    (
                        None if provider_profile_id is None else str(provider_profile_id),
                        model_override,
                        int(unattended),
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
                   AND status NOT IN ('done','failed','cancelled','budget_exceeded')
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
    ) -> RunRecord:
        if not goal.strip():
            raise ValueError("run 目标不能为空")
        if not 1 <= retrieval_top_k <= 20:
            raise ValueError("retrieval_top_k 必须位于 1 到 20")
        run_id = uuid7()
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> RunRecord:
            connection.execute(
                """
                INSERT INTO agent_runs(
                    id, conversation_id, goal, status, budget_tokens, budget_calls,
                    budget_wall_ms, answer_mode, workflow_type, schedule_id, unattended, run_trigger,
                    retrieval_top_k,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(run_id),
                    str(conversation_id),
                    goal.strip(),
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

    async def get_run(self, run_id: UUID) -> RunRecord | None:
        def operation(connection: sqlite3.Connection) -> RunRecord | None:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
            return None if row is None else self._run_record(row)

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
                   AND status IN ('queued','executing','waiting_human') LIMIT 1""",
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
            row = connection.execute(
                "SELECT next_seq FROM agent_runs WHERE id = ?", (str(run_id),)
            ).fetchone()
            if row is None:
                raise LookupError(f"run 不存在: {run_id}")
            first_seq = int(row["next_seq"])
            created_at = _iso()
            output: list[RunEvent] = []
            for offset, (event_type, payload) in enumerate(events):
                seq = first_seq + offset
                connection.execute(
                    "INSERT INTO run_events(run_id, seq, type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                    (str(run_id), seq, event_type, canonical_json(payload), created_at),
                )
                output.append(
                    RunEvent(
                        run_id, seq, event_type, payload, cast(datetime, _datetime(created_at))
                    )
                )
            connection.execute(
                "UPDATE agent_runs SET next_seq = ?, updated_at = ? WHERE id = ?",
                (first_seq + len(events), created_at, str(run_id)),
            )
            return output

        return await self._write(operation)

    async def list_events(self, *, run_id: UUID, after_seq: int = 0) -> list[RunEvent]:
        def operation(connection: sqlite3.Connection) -> list[RunEvent]:
            rows = connection.execute(
                "SELECT * FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq",
                (str(run_id), after_seq),
            ).fetchall()
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
                     AND runs.status IN ('done','failed','cancelled','budget_exceeded')
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
                allowed += ["filesystem.write", "office.word.edit", "office.excel.edit"]
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
        grant_source: Literal["user", "policy"] = "user",
        expires_in_s: int | None = None,
    ) -> Any:
        if capability not in ALL_CAPABILITIES:
            raise ValueError("未知 capability")
        if capability in PATH_CAPABILITIES and session_root_id is None:
            raise ValueError("文件 capability 必须绑定会话目录")
        if capability in GLOBAL_CAPABILITIES and session_root_id is not None:
            raise ValueError("网络/Shell/外部操作 capability 不能继承目录授权")
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
                     AND capability = ? AND revoked_at IS NULL
                     AND expires_at IS NOT NULL AND expires_at <= ?""",
                (
                    timestamp,
                    timestamp,
                    str(conversation_id),
                    None if session_root_id is None else str(session_root_id),
                    capability,
                    timestamp,
                ),
            )
            row = connection.execute(
                """SELECT * FROM capability_grants
                   WHERE conversation_id = ? AND IFNULL(session_root_id, '') = IFNULL(?, '')
                     AND capability = ? AND revoked_at IS NULL""",
                (
                    str(conversation_id),
                    None if session_root_id is None else str(session_root_id),
                    capability,
                ),
            ).fetchone()
            if row is None:
                grant_id = uuid7()
                connection.execute(
                    """INSERT INTO capability_grants(
                           id, conversation_id, session_root_id, capability, grant_source,
                           expires_at, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(grant_id),
                        str(conversation_id),
                        None if session_root_id is None else str(session_root_id),
                        capability,
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
        now = _iso()

        def operation(connection: sqlite3.Connection) -> Any:
            row = connection.execute(
                """SELECT * FROM capability_grants
                   WHERE conversation_id = ? AND capability = ? AND session_root_id IS NULL
                     AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?)
                   ORDER BY created_at DESC LIMIT 1""",
                (str(conversation_id), capability, now),
            ).fetchone()
            if row is None:
                raise CapabilityDeniedError(f"尚未授予 {capability} 权限")
            return self._grant_record(row, CapabilityGrantRecord)

        return await self._read(operation)

    async def authorize_path(
        self, *, conversation_id: UUID, target_path: Path, capability: str
    ) -> Any:
        if capability not in PATH_CAPABILITIES:
            raise ValueError("非路径 capability 必须通过 authorize_capability 校验")
        now = _iso()

        def operation(connection: sqlite3.Connection) -> Any:
            rows = connection.execute(
                """SELECT roots.id, roots.canonical_path, roots.access_mode
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
            return [self._artifact_record(row, ArtifactRecord) for row in rows]

        return await self._read(operation)

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

    async def request_cancel(self, *, run_id: UUID) -> RunRecord:
        timestamp = _iso()

        def operation(connection: sqlite3.Connection) -> RunRecord:
            changed = connection.execute(
                """UPDATE agent_runs
                   SET cancel_requested_at = COALESCE(cancel_requested_at, ?),
                       status = CASE WHEN status IN ('queued','waiting_human')
                                     THEN 'cancelled' ELSE status END,
                       finished_at = CASE WHEN status IN ('queued','waiting_human')
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
                     AND status NOT IN ('done','failed','cancelled','budget_exceeded')
                     AND (status IN ('queued','waiting_human')
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
                     AND runs.status NOT IN ('done','failed','cancelled','budget_exceeded')
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
    def _grant_record(row: sqlite3.Row, record_type: Any) -> Any:
        return record_type(
            id=UUID(row["id"]),
            conversation_id=UUID(row["conversation_id"]),
            session_root_id=None
            if row["session_root_id"] is None
            else UUID(row["session_root_id"]),
            capability=row["capability"],
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
    ) -> tuple[Any, Any]:
        memory_id = uuid7()
        timestamp = _iso()

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
                    connection.execute(
                        """UPDATE cowork_memories
                           SET content = ?, source = ?, updated_at = ? WHERE id = ?""",
                        (content, source, timestamp, str(previous.id)),
                    )
                    updated = connection.execute(
                        "SELECT * FROM cowork_memories WHERE id = ?", (str(previous.id),)
                    ).fetchone()
                    return self._memory_record(updated), previous
            connection.execute(
                """INSERT INTO cowork_memories(
                       id, scope, conversation_id, workspace_path, key, content, source,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
