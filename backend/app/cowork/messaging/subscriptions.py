"""入站：频道订阅。

一个订阅是一条持久的 `(conversation_id, channel)` 记录：某个会话主动选择**听**一个频道。
一个频道可以被多个会话订阅（两个会话、两种反应）。它一直有效，直到用户或模型显式退订；
删除会话会连带清掉它的订阅。

**这不是 Inbox 路由。** 路由把审批与提问镜像**出去**（一问一答，按 item id 相关），
订阅把频道消息带**进来**（广播）。别指到同一个频道上——那是自问自答的回路。

投递方式与自唤醒一致：会话忙就作为 steering 插进去，闲就起一个后台轮次；不需要长连接。
"""

from __future__ import annotations

from uuid import UUID

from app.core.db import DbSession as AsyncSession
from app.cowork_contracts import ChannelSubscriptionRecord, MessagingPlatform
from app.cowork_store.routing import cowork_store

_COLUMNS = """
    id, conversation_id, platform, chat_id, connector_account_id, created_at, revoked_at
"""


def _record(row: object) -> ChannelSubscriptionRecord:
    mapping = dict(row)  # type: ignore[call-overload]
    return ChannelSubscriptionRecord(**mapping)


async def subscribe_channel(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    platform: MessagingPlatform,
    chat_id: str,
    connector_account_id: UUID | None = None,
) -> ChannelSubscriptionRecord:
    store = cowork_store()
    return await store.create_channel_subscription(
        conversation_id=conversation_id,
        platform=platform,
        chat_id=chat_id,
        connector_account_id=connector_account_id,
    )


async def list_channel_subscriptions(
    session: AsyncSession,
    *,
    conversation_id: UUID | None = None,
    channel: tuple[MessagingPlatform, str] | None = None,
) -> list[ChannelSubscriptionRecord]:
    store = cowork_store()
    return await store.list_channel_subscriptions(
        conversation_id=conversation_id, channel=channel
    )


async def unsubscribe_channel(
    session: AsyncSession, *, conversation_id: UUID, subscription_id: UUID
) -> bool:
    store = cowork_store()
    return await store.revoke_channel_subscription(
        conversation_id=conversation_id, subscription_id=subscription_id
    )
