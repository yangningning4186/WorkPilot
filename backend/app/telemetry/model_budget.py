"""把 cost_reservations 协议接到模型网关上的 BudgetGuard 实现。"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.telemetry.cost_budget import release_undispatched_cost, reserve_cost, settle_cost


class SqlDailyCostGuard:
    """每日成本硬上限。

    刻意不复用请求的业务 session: 预留与结算必须独立提交, 否则业务事务回滚会把
    "已经打给 provider 的钱" 一起抹掉, 上限就形同虚设。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        limit_usd: Decimal,
        timezone: str,
        reservation_ttl_s: int,
    ) -> None:
        if reservation_ttl_s <= 0:
            raise ValueError("预留有效期必须大于 0")
        self._session_factory = session_factory
        self._limit_usd = limit_usd
        self._timezone = ZoneInfo(timezone)
        self._reservation_ttl_s = reservation_ttl_s

    async def reserve(
        self,
        *,
        idempotency_key: str,
        estimated_usd: Decimal,
        run_id: UUID | None = None,
    ) -> None:
        async with self._session_factory() as session:
            await reserve_cost(
                session,
                idempotency_key=idempotency_key,
                budget_date=self.budget_date(),
                limit_usd=self._limit_usd,
                estimated_usd=estimated_usd,
                expires_at=datetime.now(UTC) + timedelta(seconds=self._reservation_ttl_s),
                run_id=run_id,
            )

    async def settle(self, *, idempotency_key: str, actual_usd: Decimal) -> None:
        async with self._session_factory() as session:
            await settle_cost(session, idempotency_key=idempotency_key, actual_usd=actual_usd)

    async def release_undispatched(self, *, idempotency_key: str) -> None:
        async with self._session_factory() as session:
            await release_undispatched_cost(session, idempotency_key=idempotency_key)

    def budget_date(self) -> date:
        """按配置时区切日, 否则 UTC 午夜会在本地下午把额度清零。"""

        return datetime.now(self._timezone).date()


def build_cost_guard(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> SqlDailyCostGuard:
    return SqlDailyCostGuard(
        session_factory,
        limit_usd=settings.daily_cost_limit_usd,
        timezone=settings.cost_budget_timezone,
        reservation_ttl_s=settings.cost_reservation_ttl_s,
    )
