"""每日费用闸门：并发不能穿透上限，同一个幂等键只记一次账。

`test_telemetry_conformance.py` 验的是 Protocol 语义（预留→结算→释放的状态机），
这里验的是**熔断阈值**本身——两者故意分开：语义对了但阈值算错，钱照样超。
"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.telemetry.sqlite import MICRO, SqliteTelemetryStore
from workpilot_telemetry.budget import BudgetExceededError, IdempotencyConflictError

_DAY = date(2026, 8, 14)


@pytest.fixture
async def store(tmp_path: Path) -> SqliteTelemetryStore:
    created = SqliteTelemetryStore(tmp_path / "telemetry.db")
    await created.initialize()
    return created


async def _reserve(
    store: SqliteTelemetryStore, key: str, *, estimated: str, limit: str = "1.000000"
) -> object:
    return await store.reserve(
        idempotency_key=key,
        budget_date=_DAY,
        limit_usd=Decimal(limit),
        estimated_usd=Decimal(estimated),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


async def _budget(store: SqliteTelemetryStore) -> dict[str, Decimal]:
    row = await store._read(
        lambda c: c.execute(
            """SELECT reserved_micro_usd, spent_micro_usd
               FROM daily_cost_budgets WHERE budget_date = ?""",
            (_DAY.isoformat(),),
        ).fetchone()
    )
    return {
        "reserved_usd": Decimal(row["reserved_micro_usd"]) / MICRO,
        "spent_usd": Decimal(row["spent_micro_usd"]) / MICRO,
    }


async def test_concurrent_reservations_cannot_exceed_daily_limit(
    store: SqliteTelemetryStore,
) -> None:
    """两笔各 0.7 的预留、上限 1.0：必须恰好放行一笔。

    PostgreSQL 那版靠 `SELECT ... FOR UPDATE` 行锁；SQLite 靠 `BEGIN IMMEDIATE` 的
    单写者语义。换了机制之后这条性质必须重新被证明，不能假设它跟着搬过来了。
    """
    results = await asyncio.gather(
        _reserve(store, "call-a", estimated="0.700000"),
        _reserve(store, "call-b", estimated="0.700000"),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, BudgetExceededError) for result in results) == 1
    budget = await _budget(store)
    assert budget["reserved_usd"] == Decimal("0.700000")
    assert budget["spent_usd"] == Decimal("0.000000")


async def test_reservation_and_settlement_are_idempotent(
    store: SqliteTelemetryStore,
) -> None:
    """重投递不能记两次账。幂等键是唯一防线——网关会重试，队列会重投。"""
    first = await _reserve(store, "call-a", estimated="0.400000")
    again = await _reserve(store, "call-a", estimated="0.400000")

    assert first.created is True
    assert again.created is False
    assert (await _budget(store))["reserved_usd"] == Decimal("0.400000")

    await store.settle(idempotency_key="call-a", actual_usd=Decimal("0.100000"))
    await store.settle(idempotency_key="call-a", actual_usd=Decimal("0.100000"))

    budget = await _budget(store)
    assert budget["reserved_usd"] == Decimal("0.000000")
    assert budget["spent_usd"] == Decimal("0.100000")


async def test_same_idempotency_key_rejects_different_request(
    store: SqliteTelemetryStore,
) -> None:
    """同一个键对应两个不同的费用请求，是调用方算错了键，不该静默复用第一笔。"""
    await _reserve(store, "call-a", estimated="0.400000")

    with pytest.raises(IdempotencyConflictError):
        await _reserve(store, "call-a", estimated="0.500000")
