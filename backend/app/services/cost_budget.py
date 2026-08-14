from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


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


async def reserve_cost(
    session: AsyncSession,
    *,
    idempotency_key: str,
    budget_date: date,
    limit_usd: Decimal,
    estimated_usd: Decimal,
    expires_at: datetime,
    run_id: UUID | None = None,
) -> CostReservation:
    """以稳定幂等键原子预留费用；并发调用不能穿透每日上限。"""

    if estimated_usd < 0 or limit_usd < 0:
        raise ValueError("费用不能为负数")

    async with session.begin():
        await session.execute(
            text(
                """
                INSERT INTO daily_cost_budgets (budget_date, limit_usd)
                VALUES (:budget_date, :limit_usd)
                ON CONFLICT (budget_date) DO NOTHING
                """
            ),
            {"budget_date": budget_date, "limit_usd": limit_usd},
        )
        inserted = (
            await session.execute(
                text(
                    """
                    INSERT INTO cost_reservations
                        (idempotency_key, budget_date, run_id, estimated_usd,
                         status, expires_at)
                    VALUES
                        (:key, :budget_date, :run_id, :estimated_usd,
                         'reserved', :expires_at)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING idempotency_key
                    """
                ),
                {
                    "key": idempotency_key,
                    "budget_date": budget_date,
                    "run_id": run_id,
                    "estimated_usd": estimated_usd,
                    "expires_at": expires_at,
                },
            )
        ).scalar_one_or_none()

        if inserted is not None:
            reserved = (
                await session.execute(
                    text(
                        """
                        UPDATE daily_cost_budgets
                        SET reserved_usd = reserved_usd + :estimated_usd,
                            updated_at = now()
                        WHERE budget_date = :budget_date
                          AND spent_usd + reserved_usd + :estimated_usd <= limit_usd
                        RETURNING budget_date
                        """
                    ),
                    {"budget_date": budget_date, "estimated_usd": estimated_usd},
                )
            ).scalar_one_or_none()
            if reserved is None:
                raise BudgetExceededError("今日模型费用额度已用尽")
            return CostReservation(
                idempotency_key=idempotency_key,
                status="reserved",
                estimated_usd=estimated_usd,
                actual_usd=None,
                created=True,
            )

        existing = (
            (
                await session.execute(
                    text(
                        """
                    SELECT idempotency_key, budget_date, run_id, status,
                           estimated_usd, actual_usd
                    FROM cost_reservations
                    WHERE idempotency_key = :key
                    """
                    ),
                    {"key": idempotency_key},
                )
            )
            .mappings()
            .one()
        )
        if (
            existing["budget_date"] != budget_date
            or existing["run_id"] != run_id
            or existing["estimated_usd"] != estimated_usd
        ):
            raise IdempotencyConflictError("同一幂等键对应了不同的费用请求")
        return CostReservation(
            idempotency_key=existing["idempotency_key"],
            status=existing["status"],
            estimated_usd=existing["estimated_usd"],
            actual_usd=existing["actual_usd"],
            created=False,
        )


async def settle_cost(
    session: AsyncSession,
    *,
    idempotency_key: str,
    actual_usd: Decimal,
) -> CostReservation:
    """按真实费用结算；重复结算返回第一次结果。"""

    async with session.begin():
        reservation = await _lock_reservation(session, idempotency_key)
        if reservation["status"] == "settled":
            return _to_reservation(reservation, created=False)
        if reservation["status"] != "reserved":
            raise InvalidReservationTransitionError(reservation["status"])
        estimated_usd = cast(Decimal, reservation["estimated_usd"])
        if actual_usd < 0 or actual_usd > estimated_usd:
            raise ValueError("实际费用必须位于 0 与预留上限之间")

        await session.execute(
            text(
                """
                UPDATE daily_cost_budgets
                SET reserved_usd = reserved_usd - :estimated_usd,
                    spent_usd = spent_usd + :actual_usd,
                    updated_at = now()
                WHERE budget_date = :budget_date
                """
            ),
            {
                "budget_date": reservation["budget_date"],
                "estimated_usd": reservation["estimated_usd"],
                "actual_usd": actual_usd,
            },
        )
        updated = (
            (
                await session.execute(
                    text(
                        """
                    UPDATE cost_reservations
                    SET status = 'settled', actual_usd = :actual_usd, updated_at = now()
                    WHERE idempotency_key = :key
                    RETURNING idempotency_key, status, estimated_usd, actual_usd
                    """
                    ),
                    {"key": idempotency_key, "actual_usd": actual_usd},
                )
            )
            .mappings()
            .one()
        )
        return _to_reservation(dict(updated), created=False)


async def release_undispatched_cost(session: AsyncSession, *, idempotency_key: str) -> None:
    """仅供确认尚未发给 provider 的失败路径释放预留。"""

    async with session.begin():
        reservation = await _lock_reservation(session, idempotency_key)
        if reservation["status"] == "released":
            return
        if reservation["status"] != "reserved":
            raise InvalidReservationTransitionError(reservation["status"])
        await session.execute(
            text(
                """
                UPDATE daily_cost_budgets
                SET reserved_usd = reserved_usd - :estimated_usd, updated_at = now()
                WHERE budget_date = :budget_date
                """
            ),
            reservation,
        )
        await session.execute(
            text(
                """
                UPDATE cost_reservations
                SET status = 'released', updated_at = now()
                WHERE idempotency_key = :idempotency_key
                """
            ),
            reservation,
        )


async def charge_expired_estimate(session: AsyncSession, *, idempotency_key: str) -> None:
    """结果不明时按预留上限保守记账，避免错误释放可能已发生的费用。"""

    async with session.begin():
        reservation = await _lock_reservation(session, idempotency_key)
        if reservation["status"] == "charged_estimate":
            return
        expires_at = cast(datetime, reservation["expires_at"])
        if reservation["status"] != "reserved" or expires_at > datetime.now(expires_at.tzinfo):
            raise InvalidReservationTransitionError(reservation["status"])
        await session.execute(
            text(
                """
                UPDATE daily_cost_budgets
                SET reserved_usd = reserved_usd - :estimated_usd,
                    spent_usd = spent_usd + :estimated_usd,
                    updated_at = now()
                WHERE budget_date = :budget_date
                """
            ),
            reservation,
        )
        await session.execute(
            text(
                """
                UPDATE cost_reservations
                SET status = 'charged_estimate', actual_usd = estimated_usd, updated_at = now()
                WHERE idempotency_key = :idempotency_key
                """
            ),
            reservation,
        )


async def sweep_expired_reservations(session: AsyncSession, *, limit: int = 200) -> int:
    """把过期未结算的预留按上限落账, 返回处理条数。

    进程崩溃或读超时后没人来结算, 额度会被永久占住; 但也不能直接释放——那等于
    假设 provider 一定没计费。折中是到期后按预留上限记为已花费。
    """

    if limit < 1:
        raise ValueError("limit 必须大于 0")
    keys = list(
        (
            await session.execute(
                text(
                    """
                    SELECT idempotency_key
                    FROM cost_reservations
                    WHERE status = 'reserved' AND expires_at <= now()
                    ORDER BY expires_at
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            )
        )
        .scalars()
        .all()
    )
    await session.rollback()

    swept = 0
    for key in keys:
        try:
            await charge_expired_estimate(session, idempotency_key=key)
        except InvalidReservationTransitionError:
            # 并发结算已经先落地, 跳过即可。
            continue
        swept += 1
    return swept


async def _lock_reservation(session: AsyncSession, key: str) -> dict[str, object]:
    row = (
        (
            await session.execute(
                text(
                    """
                SELECT idempotency_key, budget_date, estimated_usd, actual_usd,
                       status, expires_at
                FROM cost_reservations
                WHERE idempotency_key = :key
                FOR UPDATE
                """
                ),
                {"key": key},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise LookupError(key)
    return dict(row)


def _to_reservation(row: Mapping[str, object], *, created: bool) -> CostReservation:
    estimated_usd = cast(Decimal, row["estimated_usd"])
    actual_usd = cast(Decimal | None, row["actual_usd"])
    return CostReservation(
        idempotency_key=str(row["idempotency_key"]),
        status=str(row["status"]),
        estimated_usd=estimated_usd,
        actual_usd=actual_usd,
        created=created,
    )
