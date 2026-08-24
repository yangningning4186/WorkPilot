"""消息面的 HTTP 面：出站绑定管理、入站订阅管理，以及飞书事件回调。

**事件回调是这套东西里唯一一个公网可达的入口。** 它必须在做任何解析之前先验签：
不验签就等于任何人都能伪造一条"用户批准了那条命令"。所以这个路由：

- 不走本机所有者身份（回调来自飞书，不带我们的会话），
- 但要求配置了 `encrypt_key`，且签名校验通过，
- 校验失败一律 401，且不透露失败原因。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from app.api.dependencies import get_run_bus, get_run_queue_dependency
from app.core.config import Settings, get_settings
from app.core.db import DbSession as AsyncSession
from app.core.db import get_db_session
from app.core.queue import RunQueue
from app.core.run_bus import RunBus
from app.cowork.messaging import feishu
from app.cowork.messaging.buttons import ButtonError, decode
from app.cowork.messaging.inbound import handle_card_action, route_inbound_message
from app.cowork.messaging.mentions import list_thread_sessions
from app.cowork.messaging.routing import (
    delete_inbox_binding,
    list_inbox_bindings,
    set_conversation_inbox,
    upsert_inbox_binding,
)
from app.cowork.messaging.subscriptions import (
    list_channel_subscriptions,
    subscribe_channel,
    unsubscribe_channel,
)
from app.cowork.messaging.unrouted import list_unrouted, record_unrouted
from app.schemas.messaging import (
    ChannelSubscriptionListResponse,
    ChannelSubscriptionResponse,
    ConversationInboxUpdate,
    InboxBindingListResponse,
    InboxBindingResponse,
    InboxBindingUpsert,
    SubscribeChannelRequest,
    ThreadSessionListResponse,
    ThreadSessionResponse,
    UnroutedListResponse,
    UnroutedResponse,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/messaging", tags=["messaging"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]


@router.get("/inboxes", response_model=InboxBindingListResponse)
async def get_inboxes(session: DbSession) -> InboxBindingListResponse:
    items = await list_inbox_bindings(session)
    return InboxBindingListResponse(
        items=[InboxBindingResponse.model_validate(item, from_attributes=True) for item in items]
    )


@router.put("/inboxes/{name}", response_model=InboxBindingResponse)
async def put_inbox(
    name: str, request: InboxBindingUpsert, session: DbSession
) -> InboxBindingResponse:
    try:
        binding = await upsert_inbox_binding(
            session,
            name=name,
            platform=request.platform,
            chat_id=request.chat_id,
            connector_account_id=request.connector_account_id,
            enabled=request.enabled,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    await session.commit()
    return InboxBindingResponse.model_validate(binding, from_attributes=True)


@router.delete("/inboxes/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_inbox(name: str, session: DbSession) -> Response:
    if not await delete_inbox_binding(session, name=name):
        raise HTTPException(status_code=404, detail="Inbox 不存在")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/sessions/{conversation_id}/inbox", status_code=status.HTTP_204_NO_CONTENT)
async def put_conversation_inbox(
    conversation_id: UUID, request: ConversationInboxUpdate, session: DbSession
) -> Response:
    if not await set_conversation_inbox(
        session, conversation_id=conversation_id, inbox_name=request.inbox_name
    ):
        raise HTTPException(status_code=404, detail="会话不存在")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/sessions/{conversation_id}/subscriptions",
    response_model=ChannelSubscriptionListResponse,
)
async def get_subscriptions(
    conversation_id: UUID, session: DbSession
) -> ChannelSubscriptionListResponse:
    items = await list_channel_subscriptions(session, conversation_id=conversation_id)
    return ChannelSubscriptionListResponse(
        items=[
            ChannelSubscriptionResponse.model_validate(item, from_attributes=True) for item in items
        ]
    )


@router.post(
    "/sessions/{conversation_id}/subscriptions",
    response_model=ChannelSubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_subscription(
    conversation_id: UUID, request: SubscribeChannelRequest, session: DbSession
) -> ChannelSubscriptionResponse:
    subscription = await subscribe_channel(
        session,
        conversation_id=conversation_id,
        platform=request.platform,
        chat_id=request.chat_id,
        connector_account_id=request.connector_account_id,
    )
    await session.commit()
    return ChannelSubscriptionResponse.model_validate(subscription, from_attributes=True)


@router.delete(
    "/sessions/{conversation_id}/subscriptions/{subscription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_subscription(
    conversation_id: UUID, subscription_id: UUID, session: DbSession
) -> Response:
    if not await unsubscribe_channel(
        session, conversation_id=conversation_id, subscription_id=subscription_id
    ):
        raise HTTPException(status_code=404, detail="订阅不存在")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions/{conversation_id}/threads", response_model=ThreadSessionListResponse)
async def get_threads(conversation_id: UUID, session: DbSession) -> ThreadSessionListResponse:
    items = await list_thread_sessions(session, conversation_id=conversation_id)
    return ThreadSessionListResponse(
        items=[ThreadSessionResponse.model_validate(item, from_attributes=True) for item in items]
    )


@router.get("/unrouted", response_model=UnroutedListResponse)
async def get_unrouted(session: DbSession, limit: int = 50) -> UnroutedListResponse:
    items = await list_unrouted(session, limit=max(1, min(limit, 200)))
    return UnroutedListResponse(
        items=[UnroutedResponse.model_validate(item, from_attributes=True) for item in items]
    )


@router.post("/feishu/events")
async def post_feishu_events(
    request: Request,
    session: DbSession,
    settings: AppSettings,
    queue: Annotated[RunQueue, Depends(get_run_queue_dependency)],
    bus: Annotated[RunBus, Depends(get_run_bus)],
    x_lark_request_timestamp: Annotated[str | None, Header()] = None,
    x_lark_request_nonce: Annotated[str | None, Header()] = None,
    x_lark_signature: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    encrypt_key = settings.cowork_feishu_encrypt_key
    if not encrypt_key:
        # 没配 encrypt_key 就没有验签能力。这时候接受事件等于接受任何人的事件，
        # 所以直接关掉这个入口，而不是"先跑起来再说"。
        raise HTTPException(status_code=404, detail="飞书事件回调未启用")
    body = await request.body()
    if (
        not x_lark_request_timestamp
        or not x_lark_request_nonce
        or not x_lark_signature
        or not feishu.verify_signature(
            timestamp=x_lark_request_timestamp,
            nonce=x_lark_request_nonce,
            encrypt_key=encrypt_key,
            body=body,
            signature=x_lark_signature,
        )
    ):
        # 不透露是缺头还是签名不对：那点差异足够攻击者用来试探。
        raise HTTPException(status_code=401, detail="签名校验失败")

    import json

    payload = json.loads(body or b"{}")
    if "encrypt" in payload:
        payload = feishu.decrypt_event(str(payload["encrypt"]), encrypt_key)
    # 订阅地址校验握手。
    if "challenge" in payload:
        return {"challenge": payload["challenge"]}

    event = feishu.parse_event(payload, bot_open_id=settings.cowork_feishu_bot_open_id)
    if event is None:
        return {"code": 0, "handled": False}

    if event.kind == "card_action" and event.card_value:
        try:
            item_id, resolution = decode(event.card_value)
        except ButtonError:
            await record_unrouted(
                session,
                kind="inbound",
                platform="feishu",
                chat_id=event.chat_id or None,
                summary="卡片按钮 value 无法解析",
            )
            await session.commit()
            return {"code": 0, "handled": False}

        async def requeue(*, run_id: UUID, item_id: UUID) -> None:
            await bus.publish(run_id)
            await queue.enqueue_cowork_run(run_id, attempt=item_id.int % 2_000_000_000 + 1)

        decision = await handle_card_action(
            session, item_id=item_id, resolution=resolution, requeue=requeue
        )
        await session.commit()
        return {"code": 0, "handled": True, "action": decision.action}

    async def start_run(*, conversation_id: UUID, goal: str) -> UUID:
        from app.cowork.messaging.launcher import start_cowork_run

        return await start_cowork_run(
            session,
            conversation_id=conversation_id,
            goal=goal,
            settings=settings,
            queue=queue,
            bus=bus,
        )

    async def conversation_busy(*, conversation_id: UUID) -> UUID | None:
        from app.cowork.messaging.launcher import active_run_id

        return await active_run_id(session, conversation_id=conversation_id)

    decision = await route_inbound_message(
        session,
        platform="feishu",
        chat_id=event.chat_id,
        thread_id=event.thread_id,
        body=event.text,
        mentioned_bot=event.mentioned_bot,
        start_run=start_run,
        conversation_busy=conversation_busy,
    )
    await session.commit()
    return {"code": 0, "handled": decision.action != "unrouted", "action": decision.action}
