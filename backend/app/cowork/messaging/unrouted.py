"""死信：没地方去的入站消息，和自己失败的后台轮次。

两个生产者：

- 一条入站消息（私聊、或没有任何会话订阅的频道）找不到该由谁处理；
- 一次后台轮次（频道投递、自唤醒）执行失败。

没有这张表，这两类失败就是**彻底静默**的：用户在群里 @了一句，什么都没发生，也查不到
为什么。这是可见性设施，不是队列——条目只在界面里被读到，不会被重投，所以也就不需要
状态机和重试次数。

按条数封顶：无上限地攒下去只会让本地库慢慢变大而没人看。
"""

from __future__ import annotations

import json
from typing import Any

from app.core.db import DbSession as AsyncSession
from app.cowork_contracts import MessagingPlatform, UnroutedKind, UnroutedRecord
from app.cowork_store.routing import cowork_store

# 保留多少条。够看清"最近为什么没反应"，又不至于变成一张影子日志表。
KEEP_RECENT = 200

_COLUMNS = "id, kind, platform, chat_id, summary, payload, created_at"


def _record(row: object) -> UnroutedRecord:
    mapping = dict(row)  # type: ignore[call-overload]
    payload = mapping["payload"]
    return UnroutedRecord(
        id=mapping["id"],
        kind=mapping["kind"],
        platform=mapping["platform"],
        chat_id=mapping["chat_id"],
        summary=str(mapping["summary"]),
        payload=json.loads(payload) if isinstance(payload, str) else payload,
        created_at=mapping["created_at"],
    )


async def record_unrouted(
    session: AsyncSession,
    *,
    kind: UnroutedKind,
    summary: str,
    platform: MessagingPlatform | None = None,
    chat_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> UnroutedRecord:
    body = payload or {}
    store = cowork_store()
    return await store.record_unrouted(
        kind=kind,
        platform=platform,
        chat_id=chat_id,
        summary=summary,
        payload=body,
        keep=KEEP_RECENT,
    )


async def list_unrouted(session: AsyncSession, *, limit: int = 50) -> list[UnroutedRecord]:
    store = cowork_store()
    return await store.list_unrouted(limit=limit)
