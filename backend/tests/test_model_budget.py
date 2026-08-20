from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.telemetry.cost_budget import sweep_expired_reservations
from app.telemetry.model_budget import SqlDailyCostGuard
from tests.fakes import DeterministicProvider
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.pricing import GatewayPricing, ModelPricing, estimate_tokens
from workpilot_ai.types import Message, ProviderNotDispatchedError, Usage

PRICING = GatewayPricing(
    chat=ModelPricing(input_usd_per_mtok=Decimal("1000"), output_usd_per_mtok=Decimal("2000")),
    embedding=ModelPricing(input_usd_per_mtok=Decimal("500")),
)


class ExplodingProvider(DeterministicProvider):
    def __init__(self, error: Exception) -> None:
        super().__init__(4)
        self._error = error

    async def complete(self, messages, *, max_tokens, temperature):  # type: ignore[no-untyped-def]
        raise self._error


def _guard(db_engine: AsyncEngine, limit_usd: str, *, ttl_s: int = 900) -> SqlDailyCostGuard:
    return SqlDailyCostGuard(
        async_sessionmaker(db_engine, expire_on_commit=False),
        limit_usd=Decimal(limit_usd),
        timezone="Asia/Shanghai",
        reservation_ttl_s=ttl_s,
    )


def _gateway(provider, guard) -> ModelGateway:
    return ModelGateway(
        provider,
        embedding_dimensions=4,
        budget_guard=guard,
        pricing=PRICING,
        chars_per_token=1.0,
    )


async def _budget_row(session: AsyncSession, guard: SqlDailyCostGuard):
    return (
        (
            await session.execute(
                text(
                    "SELECT limit_usd, reserved_usd, spent_usd FROM daily_cost_budgets "
                    "WHERE budget_date = :day"
                ),
                {"day": guard.budget_date()},
            )
        )
        .mappings()
        .one_or_none()
    )


def test_estimates_are_an_upper_bound_not_an_average() -> None:
    # 预留必须偏大: 估小了并发调用会一起穿透每日上限。
    assert estimate_tokens(10, chars_per_token=1.0) == 10
    assert estimate_tokens(10, chars_per_token=4.0) == 3
    with pytest.raises(ValueError):
        estimate_tokens(-1, chars_per_token=1.0)


def test_cost_is_monotonic_in_tokens_so_settlement_never_exceeds_reservation() -> None:
    pricing = ModelPricing(Decimal("3"), Decimal("15"))
    reserved = pricing.cost_usd(Usage(input_tokens=1000, output_tokens=1000))
    actual = pricing.cost_usd(Usage(input_tokens=999, output_tokens=999))
    assert actual <= reserved


def test_free_pricing_short_circuits_reservations() -> None:
    assert GatewayPricing().is_free
    assert ModelPricing().cost_usd(Usage(input_tokens=10_000)) == 0


@pytest.mark.integration
async def test_successful_call_reserves_then_settles_at_actual_usage(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    guard = _guard(db_engine, "10.00")
    gateway = _gateway(DeterministicProvider(4), guard)

    await gateway.complete([Message(role="user", content="hello")], max_tokens=100)

    budget = await _budget_row(db_session, guard)
    assert budget is not None
    # DeterministicProvider 回报 3 输入 + 2 输出 token。
    assert budget["spent_usd"] == Decimal("0.007000")
    # 预留的多余部分必须释放, 否则额度会被逐次调用蚕食干净。
    assert budget["reserved_usd"] == Decimal("0.000000")

    status = (await db_session.execute(text("SELECT status FROM cost_reservations"))).scalar_one()
    assert status == "settled"


@pytest.mark.integration
async def test_exhausted_budget_blocks_the_call_before_it_is_dispatched(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """调用前预留而不是调用后统计, 否则并发请求会一起穿透上限。"""

    from app.telemetry.cost_budget import BudgetExceededError

    provider = DeterministicProvider(4)
    gateway = _gateway(provider, _guard(db_engine, "0.000001"))

    with pytest.raises(BudgetExceededError):
        await gateway.complete([Message(role="user", content="hello")], max_tokens=100)

    # 关键断言: provider 根本没被调用, 而不是"调用了但事后记账发现超了"。
    assert provider.last_messages == []


@pytest.mark.integration
async def test_undispatched_failure_releases_the_reservation(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    guard = _guard(db_engine, "10.00")
    gateway = _gateway(ExplodingProvider(ProviderNotDispatchedError("连不上")), guard)

    with pytest.raises(ProviderNotDispatchedError):
        await gateway.complete([Message(role="user", content="hello")], max_tokens=100)

    budget = await _budget_row(db_session, guard)
    assert budget is not None
    assert budget["reserved_usd"] == Decimal("0.000000")
    assert budget["spent_usd"] == Decimal("0.000000")

    status = (await db_session.execute(text("SELECT status FROM cost_reservations"))).scalar_one()
    assert status == "released"


@pytest.mark.integration
async def test_ambiguous_failure_keeps_reservation_until_swept_at_estimate(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """读超时无法证明 provider 没计费, 因此不能释放, 只能到期按上限落账。"""

    guard = _guard(db_engine, "10.00")
    gateway = _gateway(ExplodingProvider(TimeoutError("读超时")), guard)

    with pytest.raises(TimeoutError):
        await gateway.complete([Message(role="user", content="hello")], max_tokens=100)

    budget = await _budget_row(db_session, guard)
    assert budget is not None
    estimated = budget["reserved_usd"]
    assert estimated > 0
    assert budget["spent_usd"] == Decimal("0.000000")

    # 未到期不扫。
    assert await sweep_expired_reservations(db_session) == 0

    await db_session.execute(
        text("UPDATE cost_reservations SET expires_at = now() - interval '1 second'")
    )
    await db_session.commit()
    assert await sweep_expired_reservations(db_session) == 1

    budget = await _budget_row(db_session, guard)
    assert budget is not None
    assert budget["reserved_usd"] == Decimal("0.000000")
    assert budget["spent_usd"] == estimated

    status = (await db_session.execute(text("SELECT status FROM cost_reservations"))).scalar_one()
    assert status == "charged_estimate"


@pytest.mark.integration
async def test_missing_usage_is_charged_at_the_reserved_ceiling(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """provider 不返回 usage 时按 0 计费等于免费用掉额度。"""

    class NoUsageProvider(DeterministicProvider):
        async def complete(self, messages, *, max_tokens, temperature):  # type: ignore[no-untyped-def]
            result = await super().complete(
                messages, max_tokens=max_tokens, temperature=temperature
            )
            return type(result)(
                text=result.text, model=result.model, provider=result.provider, usage=Usage()
            )

    guard = _guard(db_engine, "10.00")
    gateway = _gateway(NoUsageProvider(4), guard)

    await gateway.complete([Message(role="user", content="hello")], max_tokens=100)

    budget = await _budget_row(db_session, guard)
    assert budget is not None
    # 5 字输入 + 100 max_tokens 上限 = 0.005 + 0.2
    assert budget["spent_usd"] == Decimal("0.205000")


@pytest.mark.integration
async def test_embedding_calls_go_through_the_same_gate(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    guard = _guard(db_engine, "10.00")
    gateway = _gateway(DeterministicProvider(4), guard)

    await gateway.embed(["hello", "world"])

    budget = await _budget_row(db_session, guard)
    assert budget is not None
    assert budget["spent_usd"] > 0
    assert budget["reserved_usd"] == Decimal("0.000000")


@pytest.mark.integration
async def test_guard_reuses_the_same_daily_row_within_its_timezone(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    guard = _guard(db_engine, "10.00")
    await guard.reserve(idempotency_key="a", estimated_usd=Decimal("1.0"))
    await guard.reserve(idempotency_key="b", estimated_usd=Decimal("1.0"))

    rows = (await db_session.execute(text("SELECT count(*) FROM daily_cost_budgets"))).scalar_one()
    assert rows == 1
    expiring = (
        await db_session.execute(text("SELECT expires_at FROM cost_reservations LIMIT 1"))
    ).scalar_one()
    assert expiring > datetime.now(UTC) + timedelta(seconds=60)
