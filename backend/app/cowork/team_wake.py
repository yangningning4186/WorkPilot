"""Agent Team event 的可恢复至少一次唤醒投递。

外部 runtime 只需提供一个幂等 sink，并把 ``delivery.id`` 当幂等键。事件筛选、claim、
安全复核、ack 与 cursor CAS 都由持久 Store 承担；进程在 sink 成功后、ack 前退出时会
重投同一个 delivery id，而不是跳过事件。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.cowork_contracts import TeamWakeDeliveryRecord
from app.cowork_store.base import CoworkStore
from app.cowork_store.routing import cowork_store

TEAM_WAKE_CONSUMER = "team-wake-dispatcher"
TeamWakeSink = Callable[[TeamWakeDeliveryRecord], Awaitable[str]]


def _stable_error_code(error: Exception) -> str:
    """Outbox 只持久化稳定类型，不复制可能含 header/token/URL 的异常正文。"""

    return f"{type(error).__module__}.{type(error).__qualname__}"[:300]


async def dispatch_team_wakes_once(
    *,
    sink: TeamWakeSink,
    claim_owner: str,
    store: CoworkStore | None = None,
    limit: int = 20,
    lease_seconds: int = 30,
) -> int:
    """投递一批可用 Team wake；返回本轮完成 ack 的 feed 项数。"""

    target_store = store or cowork_store()
    claimed = await target_store.claim_team_wake_deliveries(
        consumer=TEAM_WAKE_CONSUMER,
        claim_owner=claim_owner,
        limit=limit,
        lease_seconds=lease_seconds,
    )
    acknowledged = 0
    for delivery in claimed:
        try:
            outcome = await target_store.validate_team_wake_delivery(
                delivery_id=delivery.id,
                claim_owner=claim_owner,
            )
            if outcome == "deliver":
                receipt = (await sink(delivery)).strip()
                if not receipt:
                    raise ValueError("Team wake sink 必须返回持久 delivery receipt")
            else:
                receipt = f"suppressed:{delivery.event_type}"
            await target_store.ack_team_wake_delivery(
                delivery_id=delivery.id,
                consumer=TEAM_WAKE_CONSUMER,
                claim_owner=claim_owner,
                delivery_receipt=receipt,
            )
            acknowledged += 1
        except Exception as error:
            # claim 可能恰好过期/被另一进程接管；此时 release 的 CAS 失败也不应覆盖原异常。
            try:
                await target_store.release_team_wake_delivery(
                    delivery_id=delivery.id,
                    claim_owner=claim_owner,
                    error=_stable_error_code(error),
                )
            except ValueError:
                pass
    return acknowledged


__all__ = ["TEAM_WAKE_CONSUMER", "TeamWakeSink", "dispatch_team_wakes_once"]
