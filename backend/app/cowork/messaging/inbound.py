"""入站路由：一条外部消息该由谁来处理。

判定顺序是固定的，而且**先看卡片按钮**：按钮点击自带 item id，不需要任何猜测。

1. `card_action` → 按 value 里的 item id 解析那条 inbox 请求。
2. 有会话订阅了这个频道 → 每个订阅的会话各收一份。
3. @了机器人 → 如果这条 thread 已经有归属会话就交给它，否则新开一个并绑定 thread。
4. 都不成立 → 进死信。**不能静默丢掉**：用户在群里说了一句什么都没发生、也查不到
   为什么，是这套东西最容易出现也最难排查的失败。

送进会话的方式跟自唤醒一致：会话忙就作为 steering 插进去，闲就起一个新的 run。
不需要长连接，也不要求那个会话此刻正好活着。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import structlog

from app.core.db import DbSession as AsyncSession
from app.cowork.interactions import (
    enqueue_steering,
    get_pending_inbox_item,
    resolve_inbox_item,
)
from app.cowork.messaging.mentions import bind_thread_session, get_thread_session
from app.cowork.messaging.subscriptions import list_channel_subscriptions
from app.cowork.messaging.targets import format_target
from app.cowork.messaging.unrouted import record_unrouted
from app.cowork.runtime import resume_cowork_after_human
from app.cowork_contracts import InboxRecord, MessagingPlatform
from app.cowork_store.routing import cowork_store

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class InboundDecision:
    """这条消息最终去了哪里。是给调用方和测试看的，不是控制流。"""

    action: str  # "resolved" | "steered" | "started" | "unrouted"
    conversation_ids: tuple[UUID, ...] = ()
    detail: str = ""


class Requeue(Protocol):
    """把一个已经答复过的 run 重新推进队列。由调用方注入，见 `handle_card_action`。"""

    async def __call__(self, *, run_id: UUID, item_id: UUID) -> None: ...


class RunStarter(Protocol):
    """起一个新 run 的方式由调用方注入。

    这一层不该知道预算、队列和 registry 的组装；把它们拖进来会让入站路由变成第二个
    run 创建入口，两处迟早会走偏。
    """

    async def __call__(self, *, conversation_id: UUID, goal: str) -> UUID: ...


async def _get_inbox_item(session: AsyncSession, *, item_id: UUID) -> InboxRecord | None:
    store = cowork_store()
    return await store.get_inbox_item_by_id(item_id=item_id)


async def handle_card_action(
    session: AsyncSession,
    *,
    item_id: UUID,
    resolution: str,
    requeue: Requeue,
) -> InboundDecision:
    """一次按钮点击。

    `resolution` 要么是 `approve` / `reject`，要么就是 `ask_user` 的某个选项文本本身。
    """

    item = await _get_inbox_item(session, item_id=item_id)
    if item is None:
        await record_unrouted(
            session,
            kind="inbound",
            summary=f"卡片按钮指向的请求 {item_id} 不存在",
        )
        return InboundDecision(action="unrouted", detail="item_missing")
    if item.status != "pending":
        # 重复点击不是错误：两个人同时看到同一张卡片很正常。
        return InboundDecision(action="resolved", detail="already_resolved")
    pending = await get_pending_inbox_item(
        session, run_id=item.run_id, resume_token=item.resume_token
    )
    if pending is None:
        return InboundDecision(action="resolved", detail="already_resolved")
    approved = resolution != "reject"
    answer = None if resolution in {"approve", "reject"} else resolution
    resolved, response = await resolve_inbox_item(
        session, item=pending, approved=approved, answer=answer
    )
    # 解析完必须把 run 接着推下去，否则它会一直停在 waiting_human——从用户视角看就是
    # "我明明点了批准，它却没动"。重新入队交给调用方：队列不该被拖进这一层。
    await resume_cowork_after_human(session, run_id=item.run_id, item=resolved, response=response)
    await requeue(run_id=item.run_id, item_id=item.id)
    return InboundDecision(
        action="resolved",
        conversation_ids=(item.conversation_id,),
        detail=str(response.get("status", "")),
    )


async def route_inbound_message(
    session: AsyncSession,
    *,
    platform: MessagingPlatform,
    chat_id: str,
    thread_id: str | None,
    body: str,
    mentioned_bot: bool,
    start_run: RunStarter,
    conversation_busy: BusyCheck,
) -> InboundDecision:
    subscriptions = await list_channel_subscriptions(session, channel=(platform, chat_id))
    if subscriptions:
        delivered: list[UUID] = []
        for subscription in subscriptions:
            await _wake(
                session,
                conversation_id=subscription.conversation_id,
                goal=body,
                start_run=start_run,
                conversation_busy=conversation_busy,
            )
            delivered.append(subscription.conversation_id)
        return InboundDecision(action="steered", conversation_ids=tuple(delivered))

    if mentioned_bot and thread_id:
        target = format_target(platform, chat_id, thread_id)
        existing = await get_thread_session(session, target=target)
        if existing is not None:
            await _wake(
                session,
                conversation_id=existing.conversation_id,
                goal=body,
                start_run=start_run,
                conversation_busy=conversation_busy,
            )
            return InboundDecision(action="steered", conversation_ids=(existing.conversation_id,))
        conversation_id = await _create_conversation(session)
        await bind_thread_session(
            session,
            target=target,
            conversation_id=conversation_id,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        await start_run(conversation_id=conversation_id, goal=body)
        return InboundDecision(action="started", conversation_ids=(conversation_id,))

    await record_unrouted(
        session,
        kind="inbound",
        platform=platform,
        chat_id=chat_id,
        summary="没有会话订阅这个频道，消息里也没有 @机器人",
        payload={"thread_id": thread_id, "text": body[:500]},
    )
    return InboundDecision(action="unrouted", detail="no_destination")


class BusyCheck(Protocol):
    """会话此刻有没有一个还没走完的 run；有就把消息作为 steering 插进去。"""

    async def __call__(self, *, conversation_id: UUID) -> UUID | None: ...


async def _wake(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    goal: str,
    start_run: RunStarter,
    conversation_busy: BusyCheck,
) -> None:
    """忙就 steer，闲就起一轮。和自唤醒同一条路径。"""

    active_run_id = await conversation_busy(conversation_id=conversation_id)
    if active_run_id is not None:
        await enqueue_steering(
            session,
            run_id=active_run_id,
            conversation_id=conversation_id,
            content=goal,
        )
        return
    await start_run(conversation_id=conversation_id, goal=goal)


async def _create_conversation(session: AsyncSession) -> UUID:
    from app.runstore.runs import ensure_conversation

    return await ensure_conversation(session, title="来自消息的会话")
