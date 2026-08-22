"""出站：命名 Inbox 与投递绑定。

**应用内 Inbox 永远是 store of record。** 绑定只是把同一条 item 镜像到一个聊天频道，
不是把它搬走。这条规矩决定了失败语义：投递失败只是"没镜像出去"，item 本身照样在，
用户回应用里就能处理；反过来（以频道为准）会让一次网络抖动变成一条永远没人回答的请求。

会话路由到哪个 Inbox：先看会话自己的覆盖，没有就走 `default`。没有 persona 这一层，
所以只有两级。

发送方是**注入**的（`Sender` 协议），这个模块不认识飞书、Slack 或任何具体平台。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from app.core.db import DbSession as AsyncSession
from app.cowork.messaging.buttons import Button, approval_buttons, choice_buttons
from app.cowork_contracts import InboxBindingRecord, InboxRecord, MessagingPlatform
from app.cowork_store.routing import cowork_store

DEFAULT_INBOX_NAME = "default"

_COLUMNS = "id, name, platform, chat_id, connector_account_id, enabled, created_at"


class Sender(Protocol):
    """把一条消息送到某个频道，返回平台侧的消息标识。"""

    async def __call__(
        self,
        *,
        platform: MessagingPlatform,
        chat_id: str,
        connector_account_id: UUID | None,
        text: str,
        buttons: Sequence[Button],
    ) -> str: ...


def _record(row: object) -> InboxBindingRecord:
    mapping = dict(row)  # type: ignore[call-overload]
    return InboxBindingRecord(
        id=mapping["id"],
        name=str(mapping["name"]),
        platform=mapping["platform"],
        chat_id=mapping["chat_id"],
        connector_account_id=mapping["connector_account_id"],
        enabled=bool(mapping["enabled"]),
        created_at=mapping["created_at"],
    )


async def upsert_inbox_binding(
    session: AsyncSession,
    *,
    name: str,
    platform: MessagingPlatform | None,
    chat_id: str | None,
    connector_account_id: UUID | None = None,
    enabled: bool = True,
) -> InboxBindingRecord:
    if (platform is None) != (chat_id is None):
        raise ValueError("platform 与 chat_id 必须同时给出或同时省略")
    store = cowork_store()
    return await store.upsert_inbox_binding(
        name=name,
        platform=platform,
        chat_id=chat_id,
        connector_account_id=connector_account_id,
        enabled=enabled,
    )


async def get_inbox_binding(
    session: AsyncSession, *, name: str
) -> InboxBindingRecord | None:
    store = cowork_store()
    return await store.get_inbox_binding(name=name)


async def list_inbox_bindings(session: AsyncSession) -> list[InboxBindingRecord]:
    store = cowork_store()
    return await store.list_inbox_bindings()


async def delete_inbox_binding(session: AsyncSession, *, name: str) -> bool:
    store = cowork_store()
    return await store.delete_inbox_binding(name=name)


async def set_conversation_inbox(
    session: AsyncSession, *, conversation_id: UUID, inbox_name: str | None
) -> bool:
    store = cowork_store()
    return await store.set_conversation_inbox(
        conversation_id=conversation_id, inbox_name=inbox_name
    )


async def resolve_inbox_for_conversation(
    session: AsyncSession, *, conversation_id: UUID
) -> InboxBindingRecord | None:
    """这个会话的 item 该镜像到哪里；没有绑定时返回 None。"""

    store = cowork_store()
    name = await store.get_conversation_inbox(conversation_id=conversation_id)
    binding = await get_inbox_binding(session, name=name or DEFAULT_INBOX_NAME)
    if binding is None or not binding.enabled or binding.platform is None:
        return None
    return binding


def render_item(item: InboxRecord) -> tuple[str, tuple[Button, ...]]:
    """把一条 inbox item 渲染成消息正文与按钮。

    自由文本的答复不在聊天里做：`ask_user` 若没有给出选项，这里只发通知并请用户回应用，
    因为一条自由回复既没有身份也没法校验长度与格式。
    """

    request = item.request
    if item.kind == "shell_approval":
        body = f"请求执行命令：\n{request.get('command', '')}\n原因：{request.get('reason', '')}"
        return body, approval_buttons(item.id)
    if item.kind == "external_approval":
        body = f"请求调用外部动作 {request.get('tool', '')}\n参数：{request.get('arguments', {})}"
        return body, approval_buttons(item.id)
    if item.kind == "directory_request":
        return (
            f"请求访问一个目录：{request.get('reason', '')}\n请回应用里选择目录。",
            (),
        )
    if item.kind == "capability_request":
        body = f"请求能力 {request.get('capability', '')}：{request.get('reason', '')}"
        return body, approval_buttons(item.id)
    if item.kind == "plan_approval":
        steps = request.get("steps", [])
        rendered = "\n".join(f"{index}. {step}" for index, step in enumerate(steps, 1))
        return f"提交了一个执行计划：\n{rendered}", approval_buttons(item.id)
    if item.kind == "ask_user":
        choices = request.get("choices") or []
        question = str(request.get("question", ""))
        if choices:
            return question, choice_buttons(item.id, [str(item) for item in choices])
        return f"{question}\n（这是一个开放问题，请回应用里作答。）", ()
    return f"有一条待处理请求：{item.kind}", ()


async def deliver_item(
    session: AsyncSession,
    *,
    item: InboxRecord,
    sender: Sender,
) -> str | None:
    """把一条 item 镜像到会话绑定的频道。

    投递失败不抛出：item 已经在应用内 Inbox 里，用户回应用照样能处理。让一次网络抖动
    把整个运行拖失败，换来的不是安全而是更多的中断。
    """

    binding = await resolve_inbox_for_conversation(
        session, conversation_id=item.conversation_id
    )
    if binding is None or binding.platform is None or binding.chat_id is None:
        return None
    body, buttons = render_item(item)
    try:
        delivery_ref = await sender(
            platform=binding.platform,
            chat_id=binding.chat_id,
            connector_account_id=binding.connector_account_id,
            text=body,
            buttons=buttons,
        )
    except Exception:
        return None
    store = cowork_store()
    await store.set_inbox_delivery_ref(item_id=item.id, delivery_ref=delivery_ref)
    return delivery_ref
