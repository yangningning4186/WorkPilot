import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.telemetry.cost_budget import (
    BudgetExceededError,
    IdempotencyConflictError,
    reserve_cost,
    settle_cost,
)

pytestmark = pytest.mark.integration


async def test_concurrent_reservations_cannot_exceed_daily_limit(
    db_engine: AsyncEngine, db_session
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def reserve(key: str) -> object:
        async with factory() as session:
            return await reserve_cost(
                session,
                idempotency_key=key,
                budget_date=date(2026, 8, 14),
                limit_usd=Decimal("1.000000"),
                estimated_usd=Decimal("0.700000"),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )

    results = await asyncio.gather(reserve("call-a"), reserve("call-b"), return_exceptions=True)
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, BudgetExceededError) for result in results) == 1

    async with factory() as session:
        budget = (
            await session.execute(
                text(
                    """
                    SELECT reserved_usd, spent_usd
                    FROM daily_cost_budgets
                    WHERE budget_date='2026-08-14'
                    """
                )
            )
        ).one()
    assert budget.reserved_usd == Decimal("0.700000")
    assert budget.spent_usd == Decimal("0.000000")


async def test_reservation_and_settlement_are_idempotent(db_session) -> None:
    kwargs = {
        "idempotency_key": "same-call",
        "budget_date": date(2026, 8, 14),
        "limit_usd": Decimal("1.000000"),
        "estimated_usd": Decimal("0.600000"),
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }
    first = await reserve_cost(db_session, **kwargs)
    duplicate = await reserve_cost(db_session, **kwargs)
    assert first.created is True
    assert duplicate.created is False

    settled = await settle_cost(
        db_session,
        idempotency_key="same-call",
        actual_usd=Decimal("0.250000"),
    )
    settled_again = await settle_cost(
        db_session,
        idempotency_key="same-call",
        actual_usd=Decimal("0.250000"),
    )
    assert settled.status == settled_again.status == "settled"

    budget = (
        await db_session.execute(text("SELECT reserved_usd, spent_usd FROM daily_cost_budgets"))
    ).one()
    assert budget.reserved_usd == Decimal("0.000000")
    assert budget.spent_usd == Decimal("0.250000")


async def test_same_idempotency_key_rejects_different_request(db_session) -> None:
    base = {
        "idempotency_key": "conflicting-call",
        "budget_date": date(2026, 8, 14),
        "limit_usd": Decimal("1.000000"),
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }
    await reserve_cost(db_session, estimated_usd=Decimal("0.200000"), **base)

    with pytest.raises(IdempotencyConflictError):
        await reserve_cost(db_session, estimated_usd=Decimal("0.300000"), **base)
