"""PostgreSQL -> SQLite/JSONL 的幂等回填与双读校验。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.cowork_store.jsonl import JsonlConversationStore, JsonlMessage
from app.cowork_store.sqlite import SqliteCoworkStore


@dataclass(frozen=True)
class TableMigrationResult:
    table: str
    postgres_count: int
    local_count: int
    postgres_sha256: str
    local_sha256: str

    @property
    def matches(self) -> bool:
        return self.postgres_count == self.local_count and self.postgres_sha256 == self.local_sha256


@dataclass(frozen=True)
class MigrationReport:
    ok: bool
    tables: tuple[TableMigrationResult, ...]
    messages: TableMigrationResult
    generated_at: str


@dataclass(frozen=True)
class _Spec:
    table: str
    primary_key: tuple[str, ...]
    columns: tuple[str, ...]
    where: str


_OWNER_CONVERSATION = """
    EXISTS (
        SELECT 1 FROM conversations AS owner
        WHERE owner.id = {alias}.conversation_id
          AND owner.scope = 'local_owner' AND owner.demo_session_id IS NULL
    )
"""
_RUN_CHILD = """
    EXISTS (
        SELECT 1 FROM agent_runs AS run
        JOIN conversations AS owner ON owner.id = run.conversation_id
        WHERE run.id = {alias}.run_id AND run.workflow_type = 'cowork'
          AND owner.scope = 'local_owner' AND owner.demo_session_id IS NULL
    )
"""

_SPECS = (
    _Spec(
        "conversations",
        ("id",),
        (
            "id",
            "title",
            "provider_profile_id",
            "model_override",
            "unattended",
            "archived_at",
            "summary",
            "summary_upto",
            "created_at",
            "updated_at",
        ),
        "source.scope = 'local_owner' AND source.demo_session_id IS NULL",
    ),
    _Spec(
        "agent_runs",
        ("id",),
        (
            "id",
            "conversation_id",
            "goal",
            "status",
            "worker_id",
            "lease_until",
            "heartbeat_at",
            "cancel_requested_at",
            "budget_tokens",
            "budget_calls",
            "budget_wall_ms",
            "used_tokens",
            "used_calls",
            "next_seq",
            "error",
            "answer_mode",
            "workflow_type",
            "schedule_id",
            "unattended",
            "run_trigger",
            "recovery_count",
            "started_at",
            "finished_at",
            "created_at",
            "updated_at",
        ),
        "source.workflow_type = 'cowork' AND " + _OWNER_CONVERSATION.format(alias="source"),
    ),
    _Spec(
        "run_events",
        ("run_id", "seq"),
        ("run_id", "seq", "type", "payload", "created_at"),
        _RUN_CHILD.format(alias="source"),
    ),
    _Spec(
        "agent_plan_steps",
        ("id",),
        (
            "id",
            "run_id",
            "step_idx",
            "description",
            "tool",
            "depends_on",
            "status",
            "created_at",
            "updated_at",
        ),
        _RUN_CHILD.format(alias="source"),
    ),
    _Spec(
        "agent_attempts",
        ("id",),
        (
            "id",
            "run_id",
            "plan_step_id",
            "attempt_no",
            "node",
            "tool_name",
            "tool_args",
            "tool_result",
            "status",
            "idempotency_key",
            "latency_ms",
            "tokens",
            "error_model",
            "created_at",
        ),
        _RUN_CHILD.format(alias="source"),
    ),
    _Spec(
        "agent_checkpoints",
        ("run_id", "checkpoint_id"),
        ("run_id", "checkpoint_id", "parent_id", "state", "created_at"),
        _RUN_CHILD.format(alias="source"),
    ),
    _Spec(
        "tool_invocations",
        ("idempotency_key",),
        (
            "idempotency_key",
            "run_id",
            "plan_step_id",
            "tool_name",
            "args_hash",
            "result",
            "status",
            "lease_owner",
            "lease_until",
            "retry_count",
            "effect_ref",
            "created_at",
            "updated_at",
            "completed_at",
        ),
        _RUN_CHILD.format(alias="source"),
    ),
    _Spec(
        "session_roots",
        ("id",),
        (
            "id",
            "conversation_id",
            "requested_path",
            "canonical_path",
            "label",
            "access_mode",
            "enabled",
            "created_at",
            "updated_at",
        ),
        _OWNER_CONVERSATION.format(alias="source"),
    ),
    _Spec(
        "capability_grants",
        ("id",),
        (
            "id",
            "conversation_id",
            "session_root_id",
            "capability",
            "grant_source",
            "expires_at",
            "revoked_at",
            "created_at",
            "updated_at",
        ),
        _OWNER_CONVERSATION.format(alias="source"),
    ),
    _Spec(
        "cowork_attachments",
        ("id",),
        (
            "id",
            "conversation_id",
            "message_id",
            "run_id",
            "kind",
            "filename",
            "media_type",
            "storage_path",
            "size_bytes",
            "sha256",
            "extracted_text",
            "created_at",
            "updated_at",
        ),
        _OWNER_CONVERSATION.format(alias="source"),
    ),
    _Spec(
        "artifacts",
        ("id",),
        (
            "id",
            "conversation_id",
            "run_id",
            "session_root_id",
            "kind",
            "title",
            "uri",
            "mime_type",
            "meta",
            "created_at",
            "updated_at",
        ),
        _OWNER_CONVERSATION.format(alias="source"),
    ),
    _Spec(
        "cowork_schedules",
        ("id",),
        (
            "id",
            "conversation_id",
            "title",
            "goal",
            "schedule_kind",
            "cron_expression",
            "run_at",
            "timezone",
            "enabled",
            "next_run_at",
            "last_run_at",
            "last_run_id",
            "run_count",
            "skipped_count",
            "created_at",
            "updated_at",
        ),
        _OWNER_CONVERSATION.format(alias="source"),
    ),
    _Spec(
        "cowork_inbox_items",
        ("id",),
        (
            "id",
            "run_id",
            "conversation_id",
            "kind",
            "status",
            "resume_token",
            "tool_call_id",
            "plan_step_id",
            "request",
            "response",
            "unattended",
            "created_at",
            "responded_at",
        ),
        _RUN_CHILD.format(alias="source"),
    ),
    _Spec(
        "cowork_steering_messages",
        ("id",),
        ("id", "run_id", "conversation_id", "content", "status", "created_at", "consumed_at"),
        _RUN_CHILD.format(alias="source"),
    ),
)


def _normalize(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, str) and value[:1] in {"{", "["}:
        try:
            return _normalize(json.loads(value))
        except json.JSONDecodeError:
            return value
    if isinstance(value, str) and "T" in value:
        try:
            return datetime.fromisoformat(value).isoformat(timespec="microseconds")
        except ValueError:
            return value
    return value


def _digest(rows: list[dict[str, Any]]) -> str:
    normalized = [
        json.dumps(_normalize(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for row in rows
    ]
    normalized.sort()
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def _write_report(report_path: Path, report: MigrationReport) -> None:
    report_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = report_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(report_path)
    report_path.chmod(0o600)


async def _available_columns(session: AsyncSession, table: str) -> set[str]:
    values = (
        await session.execute(
            text(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema = current_schema() AND table_name = :table"""
            ),
            {"table": table},
        )
    ).scalars()
    return {str(value) for value in values}


async def _read_spec(session: AsyncSession, spec: _Spec) -> list[dict[str, Any]]:
    available = await _available_columns(session, spec.table)
    columns = [column for column in spec.columns if column in available]
    if not set(spec.primary_key).issubset(columns):
        raise RuntimeError(f"PostgreSQL 表 {spec.table} 缺少迁移主键")
    rows = (
        (
            await session.execute(
                text(
                    f"SELECT {', '.join(f'source.{column}' for column in columns)} "
                    f"FROM {spec.table} AS source WHERE {spec.where}"
                )
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


async def migrate_and_verify(
    session: AsyncSession,
    *,
    sqlite_store: SqliteCoworkStore,
    jsonl_store: JsonlConversationStore,
    report_path: Path,
) -> MigrationReport:
    """回填后立即从两个后端独立读取并做逐表摘要校验。"""

    await sqlite_store.initialize()
    await jsonl_store.initialize()
    postgres_rows: dict[str, list[dict[str, Any]]] = {}
    for spec in _SPECS:
        rows = await _read_spec(session, spec)
        postgres_rows[spec.table] = rows
        await sqlite_store.import_rows(table=spec.table, rows=rows, primary_key=spec.primary_key)

    message_rows = (
        (
            await session.execute(
                text(
                    """SELECT messages.id, messages.conversation_id, messages.seq,
                              messages.role, messages.content, messages.status, messages.run_id,
                              messages.citations, messages.created_at
                       FROM messages
                       JOIN conversations ON conversations.id = messages.conversation_id
                       WHERE conversations.scope = 'local_owner'
                         AND conversations.demo_session_id IS NULL
                       ORDER BY messages.conversation_id, messages.seq"""
                )
            )
        )
        .mappings()
        .all()
    )
    message_snapshot = [dict(row) for row in message_rows]
    index_rows: list[dict[str, Any]] = []
    for row in message_snapshot:
        message = JsonlMessage(
            record_id=UUID(str(row["id"])),
            conversation_id=UUID(str(row["conversation_id"])),
            seq=int(row["seq"]),
            role=row["role"],
            content=str(row["content"]),
            status=row["status"],
            run_id=None if row["run_id"] is None else UUID(str(row["run_id"])),
            citations=tuple(row.get("citations") or ()),
            created_at=row["created_at"].isoformat(),
        )
        await jsonl_store.append(message)
        index_rows.append(
            {
                "record_id": row["id"],
                "conversation_id": row["conversation_id"],
                "seq": row["seq"],
                "role": row["role"],
                "status": row["status"],
                "run_id": row["run_id"],
                "created_at": row["created_at"],
            }
        )
    await sqlite_store.import_rows(
        table="conversation_message_index",
        rows=index_rows,
        primary_key=("record_id",),
    )

    table_results: list[TableMigrationResult] = []
    for spec in _SPECS:
        source = postgres_rows[spec.table]
        columns = (
            list(source[0])
            if source
            else [
                column
                for column in spec.columns
                if column in await _available_columns(session, spec.table)
            ]
        )
        local = await sqlite_store.export_rows(table=spec.table, columns=columns)
        table_results.append(
            TableMigrationResult(
                table=spec.table,
                postgres_count=len(source),
                local_count=len(local),
                postgres_sha256=_digest(source),
                local_sha256=_digest(local),
            )
        )

    local_messages: list[dict[str, Any]] = []
    conversation_ids = {UUID(str(row["conversation_id"])) for row in message_snapshot}
    for conversation_id in conversation_ids:
        for item in await jsonl_store.read(conversation_id):
            local_messages.append(
                {
                    "id": item.record_id,
                    "conversation_id": item.conversation_id,
                    "seq": item.seq,
                    "role": item.role,
                    "content": item.content,
                    "status": item.status,
                    "run_id": item.run_id,
                    "citations": list(item.citations),
                    "created_at": item.created_at,
                }
            )
    message_result = TableMigrationResult(
        table="messages.jsonl",
        postgres_count=len(message_snapshot),
        local_count=len(local_messages),
        postgres_sha256=_digest(message_snapshot),
        local_sha256=_digest(local_messages),
    )
    ok = all(item.matches for item in table_results) and message_result.matches
    report = MigrationReport(
        ok=ok,
        tables=tuple(table_results),
        messages=message_result,
        generated_at=datetime.now().astimezone().isoformat(),
    )
    await asyncio.to_thread(_write_report, report_path, report)
    if not ok:
        raise RuntimeError(f"Cowork 双读校验失败，详情见 {report_path}")
    return report
