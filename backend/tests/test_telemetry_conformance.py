"""让 SQLite 的费用闸门跑一遍包内的一致性检查。

契约与验证方式都在 `workpilot_telemetry` 里；这里只负责把真实适配器接上去。
存储从 PostgreSQL 换成 SQLite 时，这个文件只改了 fixture，**断言一行没动**——
那正是当初把 conformance 放进包里而不是写成一堆散测试的理由。
"""

from decimal import Decimal
from pathlib import Path

from app.telemetry.model_budget import DailyCostGuard
from app.telemetry.sqlite import MICRO, SqliteTelemetryStore
from workpilot_telemetry.conformance import assert_budget_guard_conforms


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
