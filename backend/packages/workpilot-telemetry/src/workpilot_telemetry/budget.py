"""费用闸门的契约与预留状态机的词汇。

`BudgetGuard` 是调用前原子预留、调用后结算的闸门协议——网关只认这个 Protocol，
不认某张 `cost_reservations` 表。`app/telemetry/cost_budget.py` 是 PostgreSQL 实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID


class BudgetExceededError(RuntimeError):
    pass


class InvalidReservationTransitionError(RuntimeError):
    pass


class IdempotencyConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class CostReservation:
    idempotency_key: str
    status: str
    estimated_usd: Decimal
    actual_usd: Decimal | None
    created: bool


class BudgetGuard(Protocol):
    """调用前原子预留、调用后结算的费用闸门(docs/12 §2.2)。

    实现必须使用独立于业务事务的连接: 钱花出去了就是花出去了, 不能随业务回滚一起消失。
    """

    async def reserve(
        self,
        *,
        idempotency_key: str,
        estimated_usd: Decimal,
        run_id: UUID | None = None,
    ) -> None:
        """预留失败时抛异常, 调用方不得再发起模型调用。"""
        ...

    async def settle(self, *, idempotency_key: str, actual_usd: Decimal) -> None: ...

    async def release_undispatched(self, *, idempotency_key: str) -> None: ...
