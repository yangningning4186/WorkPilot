"""SSE 续流: 先补历史事件, 再续实时流。"""

import json
from collections.abc import AsyncIterator
from uuid import UUID

from app.core.db import SessionFactory
from app.core.run_bus import RunBus
from app.runstore.runs import RunEvent, get_run, list_events

DEFAULT_HEARTBEAT_S = 15.0
DEFAULT_BATCH = 200


def parse_last_event_id(last_event_id: str | None) -> int | None:
    """从 `run_id:seq` 解析游标。

    浏览器 EventSource 重连时自动带上这个头, 这是 B2(断线续传)成立的机制。
    解析不出来就当没有, 由调用方回落到查询参数。
    """

    if not last_event_id or ":" not in last_event_id:
        return None
    _, _, raw_seq = last_event_id.rpartition(":")
    try:
        seq = int(raw_seq)
    except ValueError:
        return None
    return seq if seq >= 0 else None


def format_sse(event: RunEvent) -> str:
    payload = json.dumps(event.envelope(), ensure_ascii=False)
    return f"id: {event.event_id}\nevent: {event.type}\ndata: {payload}\n\n"


def format_comment(text: str) -> str:
    """SSE 注释帧: 保活用, 客户端不会当成事件。"""

    return f": {text}\n\n"


async def stream_run_events(
    session_factory: SessionFactory,
    bus: RunBus,
    *,
    run_id: UUID,
    after_seq: int = 0,
    heartbeat_s: float = DEFAULT_HEARTBEAT_S,
    batch: int = DEFAULT_BATCH,
) -> AsyncIterator[str]:
    """产出 SSE 帧, 直到 run 进入终态且事件全部发完。

    顺序很关键: **先订阅再查库**。反过来的话, "查完历史" 与 "订阅生效" 之间产生的
    事件既不在历史里也收不到通知, 客户端会永远停在半截回答上(docs/08 §3.1)。
    """

    async with bus.subscribe(run_id) as subscription:
        cursor = after_seq
        while True:
            async with session_factory() as session:
                events = await list_events(session, run_id=run_id, after_seq=cursor, limit=batch)
                run = await get_run(session, run_id)

            for event in events:
                cursor = event.seq
                yield format_sse(event)

            if run is None:
                return
            if len(events) == batch:
                # 还有积压, 不必等通知, 立刻取下一批。
                continue
            if run.is_terminal:
                return
            if not await subscription.wait(heartbeat_s):
                # 通知可能丢, 也可能只是没动静; 两种情况都靠下一轮查库兜底。
                yield format_comment("keepalive")
