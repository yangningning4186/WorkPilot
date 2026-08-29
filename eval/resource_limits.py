"""Fail-closed resource limits shared by live evaluation runners.

The model-call and token ledgers reserve capacity before dispatch so concurrent
workers cannot collectively cross a configured ceiling.  Failed or ambiguous
dispatches are charged at the conservative reservation; missing provider usage
is never interpreted as zero.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass


class EvaluationLimitExceeded(RuntimeError):
    """A live evaluation resource ceiling was reached."""

    def __init__(self, dimension: str, *, used: int | float, limit: int | float) -> None:
        self.dimension = dimension
        self.used = used
        self.limit = limit
        super().__init__(f"evaluation {dimension} fuse tripped: used={used}, limit={limit}")


@dataclass(frozen=True)
class EvaluationLimits:
    max_total_tokens: int
    max_model_calls: int
    max_wall_seconds: float

    def __post_init__(self) -> None:
        if self.max_total_tokens < 1:
            raise ValueError("max_total_tokens must be positive")
        if self.max_model_calls < 1:
            raise ValueError("max_model_calls must be positive")
        if self.max_wall_seconds <= 0:
            raise ValueError("max_wall_seconds must be positive")

    def to_dict(self) -> dict[str, int | float]:
        return asdict(self)


@dataclass(frozen=True)
class TokenReservation:
    reservation_id: int
    projected_tokens: int


class EvaluationBudget:
    """Concurrency-safe, pessimistic call/token ledger with a wall deadline."""

    def __init__(self, limits: EvaluationLimits) -> None:
        self.limits = limits
        self._started = time.monotonic()
        self._calls = 0
        self._tokens = 0
        self._reserved_tokens = 0
        self._conservative_settlements = 0
        self._next_reservation_id = 1
        self._active_reservations: dict[int, int] = {}
        self._lock = asyncio.Lock()

    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._started)

    def remaining_wall_seconds(self) -> float:
        return max(0.0, self.limits.max_wall_seconds - self.elapsed_seconds())

    def check_wall(self) -> None:
        elapsed = self.elapsed_seconds()
        if elapsed >= self.limits.max_wall_seconds:
            raise EvaluationLimitExceeded(
                "wall_seconds", used=round(elapsed, 3), limit=self.limits.max_wall_seconds
            )

    async def reserve_model_call(self, *, projected_tokens: int) -> TokenReservation:
        if projected_tokens < 1:
            raise ValueError("projected_tokens must be positive")
        async with self._lock:
            self.check_wall()
            projected_calls = self._calls + 1
            if projected_calls > self.limits.max_model_calls:
                raise EvaluationLimitExceeded(
                    "model_calls", used=projected_calls, limit=self.limits.max_model_calls
                )
            projected_total = self._tokens + self._reserved_tokens + projected_tokens
            if projected_total > self.limits.max_total_tokens:
                raise EvaluationLimitExceeded(
                    "total_tokens",
                    used=projected_total,
                    limit=self.limits.max_total_tokens,
                )
            self._calls = projected_calls
            self._reserved_tokens += projected_tokens
            reservation_id = self._next_reservation_id
            self._next_reservation_id += 1
            self._active_reservations[reservation_id] = projected_tokens
            return TokenReservation(
                reservation_id=reservation_id,
                projected_tokens=projected_tokens,
            )

    async def settle_model_call(
        self,
        reservation: TokenReservation,
        *,
        actual_tokens: int | None,
    ) -> None:
        """Settle actual usage, charging the reservation when usage is unknown."""

        projected = reservation.projected_tokens
        if actual_tokens is not None and actual_tokens < 0:
            raise ValueError("actual_tokens cannot be negative")
        charged = projected if actual_tokens is None else max(actual_tokens, 0)
        async with self._lock:
            active_projection = self._active_reservations.pop(reservation.reservation_id, None)
            if active_projection is None:
                raise RuntimeError("evaluation token reservation is unknown or already settled")
            if active_projection != projected or self._reserved_tokens < projected:
                raise RuntimeError("evaluation token reservation ledger is inconsistent")
            self._reserved_tokens -= projected
            self._tokens += charged
            if actual_tokens is None:
                self._conservative_settlements += 1
            if self._tokens + self._reserved_tokens > self.limits.max_total_tokens:
                # A provider reporting more than the conservative projection is
                # a metering-contract violation.  Surface it instead of silently
                # accepting an already-overspent run.
                raise EvaluationLimitExceeded(
                    "total_tokens",
                    used=self._tokens + self._reserved_tokens,
                    limit=self.limits.max_total_tokens,
                )

    async def snapshot(self) -> dict[str, int | float | str]:
        async with self._lock:
            return {
                "model_calls": self._calls,
                "total_tokens": self._tokens,
                "reserved_tokens": self._reserved_tokens,
                "conservative_settlements": self._conservative_settlements,
                "wall_seconds": round(self.elapsed_seconds(), 3),
                "status": "within_limits",
            }
