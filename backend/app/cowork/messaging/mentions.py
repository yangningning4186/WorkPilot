"""入站：@提及 → 会话的映射。

在一个没有会话订阅的群里 @机器人时，路由器会开一个会话并让它**拥有**那条 thread：
回复回到同一条 thread 里。这张表是去重映射，一条 thread 一条记录。

键是 thread 目标串本身（`feishu:oc_xxx:om_yyy`），和发消息、和常驻授权用的是**同一个
字符串**——一份真相服务查找、投递、授权三处。分别拼一次的话，迟早会出现"看起来一样
但比不相等"的地址：现象是消息发出去了没人收到，或者授权明明给了还在弹审批。

它也是那条"可以直接回这条 thread"授权的持久来源：重启后从这里重新推出来，
而不是指望某个进程内状态还活着。
"""

from __future__ import annotations

from uuid import UUID

from app.core.db import DbSession as AsyncSession
from app.cowork_contracts import MessagingPlatform, ThreadSessionRecord
from app.cowork_store.routing import cowork_store

_COLUMNS = "target, conversation_id, platform, chat_id, thread_id, created_at"


def _record(row: object) -> ThreadSessionRecord:
    mapping = dict(row)  # type: ignore[call-overload]
    return ThreadSessionRecord(**mapping)


async def bind_thread_session(
    session: AsyncSession,
    *,
    target: str,
    conversation_id: UUID,
    platform: MessagingPlatform,
    chat_id: str,
    thread_id: str,
) -> ThreadSessionRecord:
    store = cowork_store()
    return await store.upsert_thread_session(
        target=target,
        conversation_id=conversation_id,
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
    )


async def get_thread_session(
    session: AsyncSession, *, target: str
) -> ThreadSessionRecord | None:
    store = cowork_store()
    return await store.get_thread_session(target=target)


async def list_thread_sessions(
    session: AsyncSession, *, conversation_id: UUID
) -> list[ThreadSessionRecord]:
    store = cowork_store()
    return await store.list_thread_sessions(conversation_id=conversation_id)
