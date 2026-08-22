"""消息面的对外契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

MessagingPlatform = Literal["feishu"]


class InboxBindingResponse(BaseModel):
    id: UUID
    name: str
    platform: MessagingPlatform | None
    chat_id: str | None
    connector_account_id: UUID | None
    enabled: bool
    created_at: datetime


class InboxBindingListResponse(BaseModel):
    items: list[InboxBindingResponse]


class InboxBindingUpsert(BaseModel):
    # 两个字段必须同时给出或同时省略；只给一个是一条发不出去也报不了错的绑定。
    platform: MessagingPlatform | None = None
    chat_id: str | None = Field(default=None, max_length=200)
    connector_account_id: UUID | None = None
    enabled: bool = True


class ConversationInboxUpdate(BaseModel):
    # 空表示回到 "default"。
    inbox_name: str | None = Field(default=None, max_length=100)


class ChannelSubscriptionResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    platform: MessagingPlatform
    chat_id: str
    connector_account_id: UUID | None
    created_at: datetime
    revoked_at: datetime | None


class ChannelSubscriptionListResponse(BaseModel):
    items: list[ChannelSubscriptionResponse]


class SubscribeChannelRequest(BaseModel):
    platform: MessagingPlatform
    chat_id: str = Field(min_length=1, max_length=200)
    connector_account_id: UUID | None = None


class ThreadSessionResponse(BaseModel):
    target: str
    conversation_id: UUID
    platform: MessagingPlatform
    chat_id: str
    thread_id: str
    created_at: datetime


class ThreadSessionListResponse(BaseModel):
    items: list[ThreadSessionResponse]


class UnroutedResponse(BaseModel):
    id: UUID
    kind: Literal["inbound", "background_turn"]
    platform: MessagingPlatform | None
    chat_id: str | None
    summary: str
    payload: dict[str, Any]
    created_at: datetime


class UnroutedListResponse(BaseModel):
    items: list[UnroutedResponse]
