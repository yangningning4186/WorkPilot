"""把每日费用闸门接到模型网关上的 `BudgetGuard` 实现。

存储换成了 SQLite（`app/telemetry/sqlite.py`），这一层只负责三件 PostgreSQL 版本里
也在做的事：按配置时区切日、把日上限和预留 TTL 从 Settings 里带下来、以及**不复用业务
session**——预留与结算必须独立提交，否则业务事务一回滚，"已经打给 provider 的钱" 会跟着
被抹掉，上限就形同虚设。SQLite 版本天然满足最后这条：它有自己的连接和事务。
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.telemetry.sqlite import SqliteTelemetryStore


class DailyCostGuard:
    """每日成本硬上限。"""

    def __init__(
        self,
        store: SqliteTelemetryStore,
        *,
        limit_usd: Decimal,
        timezone: str,
        reservation_ttl_s: int,
    ) -> None:
        if reservation_ttl_s <= 0:
            raise ValueError("预留有效期必须大于 0")
        self._store = store
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
        await self._store.reserve(
            idempotency_key=idempotency_key,
            budget_date=self.budget_date(),
            limit_usd=self._limit_usd,
            estimated_usd=estimated_usd,
            expires_at=datetime.now(UTC) + timedelta(seconds=self._reservation_ttl_s),
            run_id=run_id,
        )

    async def settle(self, *, idempotency_key: str, actual_usd: Decimal) -> None:
        await self._store.settle(idempotency_key=idempotency_key, actual_usd=actual_usd)

    async def release_undispatched(self, *, idempotency_key: str) -> None:
        await self._store.release_undispatched(idempotency_key=idempotency_key)

    def budget_date(self) -> date:
        """按配置时区切日，否则 UTC 午夜会在本地下午把额度清零。"""

        return datetime.now(self._timezone).date()


def build_cost_guard(settings: Settings, store: SqliteTelemetryStore) -> DailyCostGuard:
    return DailyCostGuard(
        store,
        limit_usd=settings.daily_cost_limit_usd,
        timezone=settings.cost_budget_timezone,
        reservation_ttl_s=settings.cost_reservation_ttl_s,
    )
