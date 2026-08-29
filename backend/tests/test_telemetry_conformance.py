"""让 SQLite 的费用闸门跑一遍包内的一致性检查。

契约与验证方式都在 `workpilot_telemetry` 里；这里只负责把真实适配器接上去。
存储从 PostgreSQL 换成 SQLite 时，这个文件只改了 fixture，**断言一行没动**——
那正是当初把 conformance 放进包里而不是写成一堆散测试的理由。
"""

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.agent_core.telemetry import AgentTracer
from app.telemetry.model_budget import DailyCostGuard
from app.telemetry.sqlite import MICRO, SqliteTelemetryStore
from workpilot_telemetry.conformance import assert_budget_guard_conforms
from workpilot_telemetry.records import AUDIT_ATTRIBUTE_SCHEMA, AuditRecord, validate_audit_record
from workpilot_telemetry.spans import (
    SPAN_ATTRIBUTE_SCHEMAS,
    CompactionSpanAttributes,
    RunSpanAttributes,
    ToolSpanAttributes,
    TurnSpanAttributes,
    validate_span_attributes,
)


def test_telemetry_attribute_contracts_are_closed_and_descriptive() -> None:
    record = AuditRecord(
        trace_id="trace",
        task_type="cowork_decision",
        tier="main",
        model="model",
        provider="provider",
        input_tokens=10,
        output_tokens=5,
        latency_ms=12,
        success=True,
    )

    validate_audit_record(record)
    assert set(vars(record)) == set(AUDIT_ATTRIBUTE_SCHEMA)
    assert all(
        spec.cardinality in {"low", "medium", "high"} for spec in AUDIT_ATTRIBUTE_SCHEMA.values()
    )
    assert all(spec.python_types for spec in AUDIT_ATTRIBUTE_SCHEMA.values())
    assert set(SPAN_ATTRIBUTE_SCHEMAS) == {
        "agent.run",
        "agent.turn",
        "agent.tool",
        "agent.compaction",
    }


def test_span_contract_rejects_attribute_drift() -> None:
    with pytest.raises(ValueError, match="schema 漂移"):
        validate_span_attributes(  # type: ignore[arg-type]
            "agent.turn",
            {"kind": "turn", "iteration": 1},
        )


async def test_sqlite_daily_cost_guard_conforms(tmp_path: Path) -> None:
    store = SqliteTelemetryStore(tmp_path / "telemetry.db")
    await store.initialize()
    guard = DailyCostGuard(
        store,
        # 上限拉高：这里验的是记账语义，不是熔断阈值（那条在 test_cost_budget）。
        limit_usd=Decimal("1000.000000"),
        timezone="Asia/Shanghai",
        reservation_ttl_s=300,
    )

    async def spent() -> Decimal:
        row = await store._read(
            lambda c: c.execute(
                """SELECT reserved_micro_usd + spent_micro_usd AS total
                   FROM daily_cost_budgets WHERE budget_date = ?""",
                (guard.budget_date().isoformat(),),
            ).fetchone()
        )
        return Decimal(0) if row is None else Decimal(row["total"]) / MICRO

    await assert_budget_guard_conforms(guard, spent=spent)


async def test_agent_span_schema_persists_run_turn_tool_parent_tree(tmp_path: Path) -> None:
    store = SqliteTelemetryStore(tmp_path / "telemetry.db")
    await store.initialize()
    run_id = uuid4()
    tracer = AgentTracer(store, run_id=run_id, trace_id="trace-agent-tree")

    run_span = tracer.start("agent.run")
    turn_span = tracer.start("agent.turn")
    # Runtime persists the causal turn id with pending calls, so a tool resumed after the
    # inference ContextVar was unwound still attaches to that turn.
    await tracer.finish(
        turn_span,
        status="ok",
        attributes=TurnSpanAttributes(
            kind="turn",
            iteration=0,
            stop_reason="complete",
            model="test-model",
            provider="test-provider",
        ),
    )
    tool_span = tracer.start("agent.tool", parent_span_id=turn_span.span_id)
    await tracer.finish(
        tool_span,
        status="ok",
        attributes=ToolSpanAttributes(
            kind="tool",
            tool="read_text_file",
            tool_call_id="call-1",
            step_idx=0,
            status="ok",
        ),
    )
    compaction_span = tracer.start("agent.compaction")
    await tracer.finish(
        compaction_span,
        status="ok",
        attributes=CompactionSpanAttributes(
            kind="compaction",
            forced=False,
            changed=True,
            mode="summary",
            archived_messages=12,
            before_tokens=8_000,
            after_tokens=2_000,
            trigger_source="provider_usage",
        ),
    )
    await tracer.finish(
        run_span,
        status="ok",
        attributes=RunSpanAttributes(kind="run", workflow="cowork", status="done"),
    )

    rows = await store._read(
        lambda connection: connection.execute(
            "SELECT span_id, parent_span_id, name, attributes "
            "FROM agent_spans WHERE run_id = ? ORDER BY started_at",
            (str(run_id),),
        ).fetchall()
    )
    by_name = {row["name"]: row for row in rows}
    assert set(by_name) == {"agent.run", "agent.turn", "agent.tool", "agent.compaction"}
    assert by_name["agent.run"]["parent_span_id"] is None
    assert by_name["agent.turn"]["parent_span_id"] == run_span.span_id
    assert by_name["agent.tool"]["parent_span_id"] == turn_span.span_id
    assert by_name["agent.compaction"]["parent_span_id"] == run_span.span_id
