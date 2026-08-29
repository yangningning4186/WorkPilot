"""成本与调用审计的 SQLite 存储。

**为什么钱不能用 REAL 存。** SQLite 没有 decimal 类型。用 REAL 存美元，
`0.1 + 0.2 != 0.3` 这件事会直接发生在预算比较上——额度差一点点用不完或者刚好穿透，
而且不会有任何报错。这里统一存**整数微美元**（1e-6 USD）：精确、能在 SQL 里直接比较，
边界上和 `Decimal` 互转。这是从 PostgreSQL 的 NUMERIC 搬过来时唯一真正需要改的东西。

**并发。** 原来的实现靠 `SELECT ... FOR UPDATE` 加行锁保证"并发预留不能穿透日上限"。
SQLite 的 `BEGIN IMMEDIATE` 天然是单写者，这条性质变成免费的——所以这里的实现比
PostgreSQL 那版短，而不是长。

自己一个库文件而不是并进 `cowork.db`：调用审计是高频追加，按天聚合时会扫全表；
让它和控制面共用一个写锁，等于让一次成本看板查询卡住 Cowork 的 checkpoint 写入。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar, cast
from uuid import UUID

from uuid6 import uuid7

from workpilot_ai.types import AuditRecord
from workpilot_telemetry.budget import (
    BudgetExceededError,
    CostReservation,
    IdempotencyConflictError,
    InvalidReservationTransitionError,
)
from workpilot_telemetry.records import validate_audit_record
from workpilot_telemetry.spans import AgentSpanRecord, validate_span_attributes

T = TypeVar("T")

# 1 美元 = 1_000_000 微美元。够记到 provider 报价的最小位，也不会在 int64 里溢出。
MICRO = Decimal("1000000")

_SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS llm_calls (
    id TEXT PRIMARY KEY,
    trace_id TEXT,
    run_id TEXT,
    eval_run_id TEXT,
    task_type TEXT NOT NULL,
    cause TEXT NOT NULL DEFAULT 'primary',
    tier TEXT NOT NULL,
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 1,
    -- 微美元。NULL 表示"没有可用单价"，与 0 严格区分：本机自部署模型的单价真的是 0，
    -- 把两者折成同一个值之后，成本看板再也分不出"免费"和"没测过"（docs/07 §7.4）。
    cost_micro_usd INTEGER,
    prompt_cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    prompt_cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cached INTEGER NOT NULL DEFAULT 0,
    cache_type TEXT,
    was_fallback INTEGER NOT NULL DEFAULT 0,
    batch_id TEXT,
    span_id TEXT,
    parent_span_id TEXT,
    stop_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS llm_calls_created_at_idx ON llm_calls(created_at);
CREATE INDEX IF NOT EXISTS llm_calls_task_type_idx ON llm_calls(task_type, created_at);

CREATE TABLE IF NOT EXISTS agent_spans (
    span_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    parent_span_id TEXT,
    name TEXT NOT NULL CHECK (
        name IN ('agent.run', 'agent.turn', 'agent.tool', 'agent.compaction')
    ),
    status TEXT NOT NULL CHECK (status IN ('ok', 'error', 'cancelled')),
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    attributes TEXT NOT NULL,
    error_type TEXT
);

CREATE INDEX IF NOT EXISTS agent_spans_run_idx ON agent_spans(run_id, started_at);
CREATE INDEX IF NOT EXISTS agent_spans_parent_idx ON agent_spans(parent_span_id);

CREATE TABLE IF NOT EXISTS daily_cost_budgets (
    budget_date TEXT PRIMARY KEY,
    limit_micro_usd INTEGER NOT NULL,
    reserved_micro_usd INTEGER NOT NULL DEFAULT 0,
    spent_micro_usd INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_reservations (
    idempotency_key TEXT PRIMARY KEY,
    budget_date TEXT NOT NULL,
    run_id TEXT,
    estimated_micro_usd INTEGER NOT NULL,
    actual_micro_usd INTEGER,
    -- charged_estimate 与 settled 必须分开：前者是"没人来结算，按预留上限扣的"，
    -- 后者是"provider 报了真实用量"。合并之后就再也看不出有多少钱是估出来的。
    status TEXT NOT NULL
        CHECK (status IN ('reserved', 'settled', 'released', 'charged_estimate')),
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS cost_reservations_sweep_idx
    ON cost_reservations(status, expires_at);
"""


def to_micro(value: Decimal) -> int:
    """Decimal 美元 → 整数微美元。截断而不是四舍五入：宁可少记一点点也不要凭空多记。"""
    return int((value * MICRO).to_integral_value(rounding="ROUND_DOWN"))


def from_micro(value: int | None) -> Decimal | None:
    return None if value is None else Decimal(value) / MICRO


def _now() -> str:
    """一律 UTC ISO。

    时间在 SQLite 里是字符串，比较是**字典序**。混用本地偏移和 UTC 会让
    `2026-08-21T22:59+08:00` 排在 `2026-08-21T15:05+00:00` 后面——两者其实是同一刻。
    过期预留的清扫就是靠这个比较，混了就会把没到期的一起扫掉。
    """
    return datetime.now(UTC).isoformat()


class SqliteTelemetryStore:
    """调用审计 + 每日费用闸门。同时满足 `AuditSink` 与 `BudgetGuard` 两个 Protocol。"""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.executescript(_SCHEMA)
            existing = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(llm_calls)").fetchall()
            }
            for column in ("span_id", "parent_span_id", "stop_reason"):
                if column not in existing:
                    connection.execute(f"ALTER TABLE llm_calls ADD COLUMN {column} TEXT")
            if "cause" not in existing:
                connection.execute(
                    "ALTER TABLE llm_calls ADD COLUMN cause TEXT NOT NULL DEFAULT 'primary'"
                )
                connection.execute(
                    """UPDATE llm_calls SET cause = CASE
                           WHEN task_type = 'cowork_compaction' THEN 'compaction'
                           WHEN task_type IN (
                               'conversation_title','memory_op','skill_distillation',
                               'cowork_semantic_approval'
                           ) THEN 'hook'
                           ELSE 'primary' END"""
                )
            span_schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'agent_spans'"
            ).fetchone()
            if span_schema is not None and "agent.compaction" not in str(span_schema["sql"]):
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute("DROP INDEX IF EXISTS agent_spans_run_idx")
                    connection.execute("DROP INDEX IF EXISTS agent_spans_parent_idx")
                    connection.execute("ALTER TABLE agent_spans RENAME TO agent_spans_legacy")
                    connection.execute(
                        """CREATE TABLE agent_spans (
                               span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL,
                               run_id TEXT NOT NULL, parent_span_id TEXT,
                               name TEXT NOT NULL CHECK (name IN (
                                   'agent.run','agent.turn','agent.tool','agent.compaction'
                               )),
                               status TEXT NOT NULL CHECK (status IN ('ok','error','cancelled')),
                               started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
                               duration_ms INTEGER NOT NULL, attributes TEXT NOT NULL,
                               error_type TEXT
                           )"""
                    )
                    connection.execute(
                        """INSERT INTO agent_spans
                           SELECT * FROM agent_spans_legacy"""
                    )
                    connection.execute("DROP TABLE agent_spans_legacy")
                    connection.execute(
                        "CREATE INDEX agent_spans_run_idx ON agent_spans(run_id, started_at)"
                    )
                    connection.execute(
                        "CREATE INDEX agent_spans_parent_idx ON agent_spans(parent_span_id)"
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
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
                    # IMMEDIATE：写事务一开始就拿排他锁。"并发预留不能穿透日上限"
                    # 这条性质就是靠它，不需要 PostgreSQL 那边的 FOR UPDATE 行锁。
                    connection.execute("BEGIN IMMEDIATE")
                    value = operation(connection)
                    connection.execute("COMMIT")
                    return value
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                finally:
                    connection.close()

            return await asyncio.to_thread(run)

    # -- AuditSink ---------------------------------------------------------

    async def record(self, call: AuditRecord) -> None:
        validate_audit_record(call)
        payload = vars(call)
        cost = payload.get("cost_usd")
        row = {
            "id": str(uuid7()),
            "trace_id": payload.get("trace_id"),
            "run_id": None if payload.get("run_id") is None else str(payload["run_id"]),
            "eval_run_id": (
                None if payload.get("eval_run_id") is None else str(payload["eval_run_id"])
            ),
            "task_type": payload["task_type"],
            "cause": payload["cause"],
            "tier": payload["tier"],
            "model": payload["model"],
            "provider": payload["provider"],
            "prompt_tokens": payload.get("input_tokens", 0),
            "output_tokens": payload.get("output_tokens", 0),
            "latency_ms": payload.get("latency_ms", 0),
            "success": int(bool(payload.get("success", True))),
            "cost_micro_usd": None if cost is None else to_micro(Decimal(str(cost))),
            "prompt_cache_read_tokens": payload.get("prompt_cache_read_tokens", 0),
            "prompt_cache_write_tokens": payload.get("prompt_cache_write_tokens", 0),
            "cached": int(bool(payload.get("cached", False))),
            "cache_type": payload.get("cache_type"),
            "was_fallback": int(bool(payload.get("was_fallback", False))),
            "batch_id": None if payload.get("batch_id") is None else str(payload["batch_id"]),
            "span_id": payload.get("span_id"),
            "parent_span_id": payload.get("parent_span_id"),
            "stop_reason": payload.get("stop_reason"),
            "created_at": _now(),
        }
        await self._write(
            lambda connection: connection.execute(
                """
                INSERT INTO llm_calls (
                    id, trace_id, run_id, eval_run_id, task_type, cause, tier, model, provider,
                    prompt_tokens, output_tokens, latency_ms, success, cost_micro_usd,
                    prompt_cache_read_tokens, prompt_cache_write_tokens,
                    cached, cache_type, was_fallback, batch_id, span_id, parent_span_id,
                    stop_reason, created_at
                ) VALUES (
                    :id, :trace_id, :run_id, :eval_run_id, :task_type, :cause, :tier, :model, :provider,
                    :prompt_tokens, :output_tokens, :latency_ms, :success, :cost_micro_usd,
                    :prompt_cache_read_tokens, :prompt_cache_write_tokens,
                    :cached, :cache_type, :was_fallback, :batch_id, :span_id, :parent_span_id,
                    :stop_reason, :created_at
                )
                """,
                row,
            )
        )

    async def record_span(self, span: AgentSpanRecord) -> None:
        validate_span_attributes(span.name, span.attributes)
        row = {
            "span_id": span.span_id,
            "trace_id": span.trace_id,
            "run_id": str(span.run_id),
            "parent_span_id": span.parent_span_id,
            "name": span.name,
            "status": span.status,
            "started_at": span.started_at,
            "ended_at": span.ended_at,
            "duration_ms": span.duration_ms,
            "attributes": json.dumps(
                span.attributes,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ),
            "error_type": span.error_type,
        }
        await self._write(
            lambda connection: connection.execute(
                """INSERT INTO agent_spans (
                       span_id, trace_id, run_id, parent_span_id, name, status,
                       started_at, ended_at, duration_ms, attributes, error_type
                   ) VALUES (
                       :span_id, :trace_id, :run_id, :parent_span_id, :name, :status,
                       :started_at, :ended_at, :duration_ms, :attributes, :error_type
                   )""",
                row,
            )
        )

    # -- BudgetGuard -------------------------------------------------------

    async def reserve(
        self,
        *,
        idempotency_key: str,
        budget_date: date,
        limit_usd: Decimal,
        estimated_usd: Decimal,
        expires_at: datetime,
        run_id: UUID | None = None,
    ) -> CostReservation:
        if estimated_usd < 0 or limit_usd < 0:
            raise ValueError("费用不能为负数")
        estimated = to_micro(estimated_usd)
        limit = to_micro(limit_usd)
        day = budget_date.isoformat()

        def operation(connection: sqlite3.Connection) -> CostReservation:
            connection.execute(
                """INSERT OR IGNORE INTO daily_cost_budgets
                       (budget_date, limit_micro_usd, updated_at)
                   VALUES (?, ?, ?)""",
                (day, limit, _now()),
            )
            existing = connection.execute(
                """SELECT idempotency_key, budget_date, run_id, status,
                          estimated_micro_usd, actual_micro_usd
                   FROM cost_reservations WHERE idempotency_key = ?""",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["budget_date"] != day
                    or existing["run_id"] != (None if run_id is None else str(run_id))
                    or existing["estimated_micro_usd"] != estimated
                ):
                    raise IdempotencyConflictError("同一幂等键对应了不同的费用请求")
                return _reservation(existing, created=False)

            budget = connection.execute(
                """SELECT limit_micro_usd, reserved_micro_usd, spent_micro_usd
                   FROM daily_cost_budgets WHERE budget_date = ?""",
                (day,),
            ).fetchone()
            headroom = (
                budget["limit_micro_usd"] - budget["reserved_micro_usd"] - budget["spent_micro_usd"]
            )
            if estimated > headroom:
                raise BudgetExceededError("今日模型费用额度已用尽")
            connection.execute(
                """INSERT INTO cost_reservations
                       (idempotency_key, budget_date, run_id, estimated_micro_usd,
                        status, expires_at, updated_at)
                   VALUES (?, ?, ?, ?, 'reserved', ?, ?)""",
                (
                    idempotency_key,
                    day,
                    None if run_id is None else str(run_id),
                    estimated,
                    expires_at.astimezone(UTC).isoformat(),
                    _now(),
                ),
            )
            connection.execute(
                """UPDATE daily_cost_budgets
                   SET reserved_micro_usd = reserved_micro_usd + ?, updated_at = ?
                   WHERE budget_date = ?""",
                (estimated, _now(), day),
            )
            return CostReservation(
                idempotency_key=idempotency_key,
                status="reserved",
                estimated_usd=estimated_usd,
                actual_usd=None,
                created=True,
            )

        return await self._write(operation)

    async def settle(self, *, idempotency_key: str, actual_usd: Decimal) -> None:
        actual = to_micro(actual_usd)

        def operation(connection: sqlite3.Connection) -> None:
            row = _require(connection, idempotency_key)
            if row["status"] == "settled":
                return
            if row["status"] != "reserved":
                raise InvalidReservationTransitionError(row["status"])
            if actual < 0 or actual > row["estimated_micro_usd"]:
                raise ValueError("实际费用必须位于 0 与预留上限之间")
            connection.execute(
                """UPDATE daily_cost_budgets
                   SET reserved_micro_usd = reserved_micro_usd - ?,
                       spent_micro_usd = spent_micro_usd + ?, updated_at = ?
                   WHERE budget_date = ?""",
                (row["estimated_micro_usd"], actual, _now(), row["budget_date"]),
            )
            connection.execute(
                """UPDATE cost_reservations
                   SET status = 'settled', actual_micro_usd = ?, updated_at = ?
                   WHERE idempotency_key = ?""",
                (actual, _now(), idempotency_key),
            )

        await self._write(operation)

    async def release_undispatched(self, *, idempotency_key: str) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            row = _require(connection, idempotency_key)
            if row["status"] == "released":
                return
            if row["status"] != "reserved":
                raise InvalidReservationTransitionError(row["status"])
            connection.execute(
                """UPDATE daily_cost_budgets
                   SET reserved_micro_usd = reserved_micro_usd - ?, updated_at = ?
                   WHERE budget_date = ?""",
                (row["estimated_micro_usd"], _now(), row["budget_date"]),
            )
            connection.execute(
                """UPDATE cost_reservations SET status = 'released', updated_at = ?
                   WHERE idempotency_key = ?""",
                (_now(), idempotency_key),
            )

        await self._write(operation)

    async def sweep_expired(self, *, limit: int = 200) -> int:
        """过期未结算的预留按预留上限落账。

        直接释放等于假设 provider 一定没计费——进程崩在"请求已发出、响应没回来"那一刻
        时，那个假设是错的。按上限落账是保守的一侧。
        """
        if limit < 1:
            raise ValueError("limit 必须大于 0")

        def operation(connection: sqlite3.Connection) -> int:
            rows = connection.execute(
                """SELECT idempotency_key, budget_date, estimated_micro_usd
                   FROM cost_reservations
                   WHERE status = 'reserved' AND expires_at <= ?
                   ORDER BY expires_at LIMIT ?""",
                (_now(), limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """UPDATE daily_cost_budgets
                       SET reserved_micro_usd = reserved_micro_usd - ?,
                           spent_micro_usd = spent_micro_usd + ?, updated_at = ?
                       WHERE budget_date = ?""",
                    (
                        row["estimated_micro_usd"],
                        row["estimated_micro_usd"],
                        _now(),
                        row["budget_date"],
                    ),
                )
                connection.execute(
                    """UPDATE cost_reservations
                       SET status = 'charged_estimate', actual_micro_usd = ?, updated_at = ?
                       WHERE idempotency_key = ?""",
                    (row["estimated_micro_usd"], _now(), row["idempotency_key"]),
                )
            return len(rows)

        return await self._write(operation)

    async def spent_usd(self, *, budget_date: date) -> Decimal:
        row = await self._read(
            lambda connection: connection.execute(
                "SELECT spent_micro_usd FROM daily_cost_budgets WHERE budget_date = ?",
                (budget_date.isoformat(),),
            ).fetchone()
        )
        return Decimal(0) if row is None else Decimal(row["spent_micro_usd"]) / MICRO

    # -- 成本看板 ----------------------------------------------------------

    async def usage_since(self, *, days: int) -> list[dict[str, Any]]:
        """按 task_type + tier 聚合最近 N 天的调用。

        `cost_micro_usd IS NULL` 的调用单独计数而不是当 0：那是"没有可用单价"，
        看板必须显示成"不可用"。
        """
        cutoff = f"-{int(days)} days"
        rows = await self._read(
            lambda connection: connection.execute(
                """SELECT task_type, tier, model, provider,
                          COUNT(*) AS calls,
                          SUM(prompt_tokens) AS prompt_tokens,
                          SUM(output_tokens) AS output_tokens,
                          SUM(CASE WHEN cached THEN 1 ELSE 0 END) AS cached_calls,
                          SUM(CASE WHEN success THEN 0 ELSE 1 END) AS failed_calls,
                          SUM(CASE WHEN cost_micro_usd IS NULL THEN 1 ELSE 0 END)
                              AS unpriced_calls,
                          SUM(COALESCE(cost_micro_usd, 0)) AS cost_micro_usd
                   FROM llm_calls
                   WHERE created_at >= datetime('now', ?)
                   GROUP BY task_type, tier, model, provider
                   ORDER BY cost_micro_usd DESC, calls DESC""",
                (cutoff,),
            ).fetchall()
        )
        return [
            {**dict(row), "cost_usd": Decimal(row["cost_micro_usd"] or 0) / MICRO} for row in rows
        ]


def _require(connection: sqlite3.Connection, key: str) -> sqlite3.Row:
    row = connection.execute(
        """SELECT idempotency_key, budget_date, estimated_micro_usd, actual_micro_usd,
                  status, expires_at
           FROM cost_reservations WHERE idempotency_key = ?""",
        (key,),
    ).fetchone()
    if row is None:
        raise LookupError(key)
    return cast("sqlite3.Row", row)


def _reservation(row: sqlite3.Row, *, created: bool) -> CostReservation:
    return CostReservation(
        idempotency_key=str(row["idempotency_key"]),
        status=str(row["status"]),
        estimated_usd=Decimal(row["estimated_micro_usd"]) / MICRO,
        actual_usd=from_micro(row["actual_micro_usd"]),
        created=created,
    )


__all__ = ["MICRO", "SqliteTelemetryStore", "from_micro", "to_micro"]
