"""让 PostgreSQL 的费用闸门跑一遍包内的一致性检查。

契约与验证方式都在 `workpilot_telemetry` 里；这里只负责把真实适配器接上去。
以后桌面 SQLite 出第二个实现时，这个文件复制一份换 fixture 即可，
断言一行都不用重写——这正是把 conformance 放进包里的理由。
"""

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.telemetry.model_budget import SqlDailyCostGuard
from workpilot_telemetry.conformance import assert_budget_guard_conforms

pytestmark = pytest.mark.integration


async def test_sql_daily_cost_guard_conforms(db_engine: AsyncEngine) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    guard = SqlDailyCostGuard(
        factory,
        # 上限拉高：这里验的是记账语义，不是熔断阈值（那条在 test_cost_budget）。
        limit_usd=Decimal("1000.000000"),
        timezone="Asia/Shanghai",
        reservation_ttl_s=300,
    )

    async def spent() -> Decimal:
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT reserved_usd + spent_usd AS total
                        FROM daily_cost_budgets WHERE budget_date = :day
                        """
                    ),
                    {"day": guard.budget_date()},
                )
            ).one_or_none()
        return Decimal(0) if row is None else Decimal(row.total)

    await assert_budget_guard_conforms(guard, spent=spent)
