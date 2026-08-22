"""消息面：审批送出去、频道消息带进来、以及两者都不成立时不静默丢掉。

这套东西最容易出现也最难排查的失败是"用户在群里说了一句，什么都没发生，还查不到为什么"。
所以这里花在死信和地址串上的用例，和花在正常路径上的一样多。
"""

from uuid import UUID, uuid4

import pytest

from app.core.db import DbSession as AsyncSession
from app.cowork.messaging.buttons import (
    ButtonError,
    approval_buttons,
    choice_buttons,
    decode,
    encode,
)
from app.cowork.messaging.feishu import parse_event, render_message, verify_signature
from app.cowork.messaging.inbound import route_inbound_message
from app.cowork.messaging.mentions import get_thread_session
from app.cowork.messaging.routing import (
    DEFAULT_INBOX_NAME,
    deliver_item,
    render_item,
    resolve_inbox_for_conversation,
    set_conversation_inbox,
    upsert_inbox_binding,
)
from app.cowork.messaging.subscriptions import (
    list_channel_subscriptions,
    subscribe_channel,
    unsubscribe_channel,
)
from app.cowork.messaging.targets import TargetError, format_target, parse_target
from app.cowork.messaging.unrouted import list_unrouted
from app.cowork_contracts import InboxRecord
from app.runstore.runs import ensure_conversation

# ---- 地址串 -----------------------------------------------------------------


def test_targets_round_trip_and_reject_ambiguous_segments() -> None:
    """同一个字符串要服务查找、投递、授权三处。

    分别拼一次的话，迟早会出现"看起来一样但比不相等"的地址：现象是消息发出去了没人收到，
    或者授权明明给了还在弹审批。
    """

    channel = format_target("feishu", "oc_123")
    assert parse_target(channel) == ("feishu", "oc_123", None)
    thread = format_target("feishu", "oc_123", "om_456")
    assert parse_target(thread) == ("feishu", "oc_123", "om_456")

    # chat_id 里带冒号会让解析变成猜谜。
    with pytest.raises(TargetError, match="冒号"):
        format_target("feishu", "oc:123")
    with pytest.raises(TargetError, match="不支持"):
        parse_target("slack:C0123")


# ---- 按钮 -------------------------------------------------------------------


def test_button_values_carry_their_own_identity() -> None:
    """按钮点击自带 item id：不靠回复文本里的标记，也不靠 thread 关系反查。"""

    item_id = uuid4()
    value = encode(item_id, "approve")
    assert decode(value) == (item_id, "approve")
    # 编码结果必须是纯 ASCII：各家平台对引号花括号的转义各不相同。
    assert value.isascii()


def test_a_corrupted_button_value_is_an_error_not_a_guess() -> None:
    with pytest.raises(ButtonError):
        decode("not-a-real-value")


def test_overlong_choices_are_dropped_rather_than_truncated() -> None:
    """截断后的按钮点下去，用户以为自己选的是另一件事。"""

    item_id = uuid4()
    buttons = choice_buttons(item_id, ["短选项", "x" * 5000])
    assert [button.label for button in buttons] == ["短选项"]


# ---- 飞书适配器 --------------------------------------------------------------


def test_signature_verification_rejects_a_forged_event() -> None:
    """回调地址是公网可达的。不验签就等于任何人都能伪造"用户批准了那条命令"。"""

    body = b'{"hello":"world"}'
    assert verify_signature(
        timestamp="1",
        nonce="n",
        encrypt_key="k",
        body=body,
        signature=_expected_signature("1", "n", "k", body),
    )
    assert not verify_signature(
        timestamp="1", nonce="n", encrypt_key="k", body=body, signature="0" * 64
    )
    # 换掉 body 而签名不变也必须失败，否则重放一条签名就能塞进任意内容。
    assert not verify_signature(
        timestamp="1",
        nonce="n",
        encrypt_key="k",
        body=b'{"hello":"evil"}',
        signature=_expected_signature("1", "n", "k", body),
    )


def _expected_signature(timestamp: str, nonce: str, key: str, body: bytes) -> str:
    import hashlib

    return hashlib.sha256(
        timestamp.encode() + nonce.encode() + key.encode() + body
    ).hexdigest()


def test_message_events_anchor_on_the_thread_root_not_the_parent() -> None:
    """用 parent_id 做键会让同一条 thread 里的两条回复落到两个 "thread" 上。"""

    event = parse_event(
        {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_1"}},
                "message": {
                    "chat_id": "oc_1",
                    "root_id": "om_root",
                    "parent_id": "om_parent",
                    "message_id": "om_self",
                    "content": '{"text": "@bot 帮我看下"}',
                    "mentions": [{"id": {"open_id": "ou_bot"}}],
                },
            },
        },
        bot_open_id="ou_bot",
    )
    assert event is not None
    assert event.thread_id == "om_root"
    assert event.mentioned_bot is True
    assert event.text == "@bot 帮我看下"


def test_a_mention_of_someone_else_is_not_a_mention_of_the_bot() -> None:
    event = parse_event(
        {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "message": {
                    "chat_id": "oc_1",
                    "root_id": "om_root",
                    "content": '{"text": "@别人"}',
                    "mentions": [{"id": {"open_id": "ou_other"}}],
                }
            },
        },
        bot_open_id="ou_bot",
    )
    assert event is not None
    assert event.mentioned_bot is False


def test_unknown_events_are_ignored_rather_than_guessed() -> None:
    assert parse_event({"header": {"event_type": "im.chat.updated_v1"}}, bot_open_id=None) is None


def test_plain_text_when_there_are_no_buttons() -> None:
    """一张只有正文的卡片在手机通知里反而更难读。"""

    msg_type, content = render_message("你好", ())
    assert msg_type == "text"
    assert content == {"text": "你好"}
    msg_type, content = render_message("批准吗", approval_buttons(uuid4()))
    assert msg_type == "interactive"


# ---- 渲染 -------------------------------------------------------------------


def test_open_questions_do_not_get_buttons() -> None:
    """自由文本的答复不在聊天里做：一条自由回复既没有身份也没法校验格式。"""

    item = _item(kind="ask_user", request={"question": "选哪个方案？", "choices": []})
    body, buttons = render_item(item)
    assert buttons == ()
    assert "请回应用里作答" in body

    item = _item(kind="ask_user", request={"question": "选哪个？", "choices": ["A", "B"]})
    _, buttons = render_item(item)
    assert [button.label for button in buttons] == ["A", "B"]


def test_directory_requests_never_get_an_approve_button() -> None:
    """目录请求要选一个具体目录，不是一个是非题。"""

    item = _item(kind="directory_request", request={"reason": "要读报告"})
    _, buttons = render_item(item)
    assert buttons == ()


def _item(*, kind: str, request: dict) -> InboxRecord:
    from datetime import UTC, datetime

    return InboxRecord(
        id=uuid4(),
        run_id=uuid4(),
        conversation_id=uuid4(),
        kind=kind,  # type: ignore[arg-type]
        status="pending",
        resume_token=uuid4(),
        tool_call_id="call-1",
        plan_step_id=uuid4(),
        request=request,
        response=None,
        created_at=datetime.now(UTC),
        responded_at=None,
        unattended=False,
    )


# ---- 出站路由 ---------------------------------------------------------------


@pytest.mark.integration
async def test_a_conversation_without_a_binding_delivers_nothing(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(
        db_session, title="No binding"
    )
    assert (
        await resolve_inbox_for_conversation(db_session, conversation_id=conversation_id)
    ) is None


@pytest.mark.integration
async def test_delivery_falls_back_to_the_default_inbox_and_honours_the_override(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(
        db_session, title="Routing"
    )
    await upsert_inbox_binding(
        db_session, name=DEFAULT_INBOX_NAME, platform="feishu", chat_id="oc_default"
    )
    await upsert_inbox_binding(
        db_session, name="oncall", platform="feishu", chat_id="oc_oncall"
    )
    await db_session.commit()

    binding = await resolve_inbox_for_conversation(
        db_session, conversation_id=conversation_id
    )
    assert binding is not None and binding.chat_id == "oc_default"

    await set_conversation_inbox(
        db_session, conversation_id=conversation_id, inbox_name="oncall"
    )
    await db_session.commit()
    binding = await resolve_inbox_for_conversation(
        db_session, conversation_id=conversation_id
    )
    assert binding is not None and binding.chat_id == "oc_oncall"


@pytest.mark.integration
async def test_a_failing_sender_never_fails_the_run(db_session: AsyncSession) -> None:
    """item 已经在应用内 Inbox 里。让一次网络抖动把运行拖失败，换来的不是安全而是中断。"""

    conversation_id = await ensure_conversation(
        db_session, title="Sender failure"
    )
    await upsert_inbox_binding(
        db_session, name=DEFAULT_INBOX_NAME, platform="feishu", chat_id="oc_1"
    )
    await db_session.commit()

    async def broken_sender(**_: object) -> str:
        raise RuntimeError("网络断了")

    item = _item(kind="shell_approval", request={"command": "ls", "reason": "看看"})
    object.__setattr__(item, "conversation_id", conversation_id)
    assert await deliver_item(db_session, item=item, sender=broken_sender) is None


# ---- 入站路由 ---------------------------------------------------------------


@pytest.mark.integration
async def test_a_subscribed_channel_wakes_every_subscribed_conversation(
    db_session: AsyncSession,
) -> None:
    first = await ensure_conversation(db_session, title="Sub A")
    second = await ensure_conversation(db_session, title="Sub B")
    for conversation_id in (first, second):
        await subscribe_channel(
            db_session,
            conversation_id=conversation_id,
            platform="feishu",
            chat_id="oc_shared",
        )
    await db_session.commit()

    started: list[UUID] = []

    async def start_run(*, conversation_id: UUID, goal: str) -> UUID:
        started.append(conversation_id)
        return uuid4()

    async def conversation_busy(*, conversation_id: UUID) -> UUID | None:
        return None

    decision = await route_inbound_message(
        db_session,
        platform="feishu",
        chat_id="oc_shared",
        thread_id="om_1",
        body="早上好",
        mentioned_bot=False,
        start_run=start_run,
        conversation_busy=conversation_busy,
    )
    assert decision.action == "steered"
    assert set(started) == {first, second}


@pytest.mark.integration
async def test_a_mention_in_an_unsubscribed_channel_opens_a_session_that_owns_the_thread(
    db_session: AsyncSession,
) -> None:
    started: list[UUID] = []

    async def start_run(*, conversation_id: UUID, goal: str) -> UUID:
        started.append(conversation_id)
        return uuid4()

    async def conversation_busy(*, conversation_id: UUID) -> UUID | None:
        return None

    decision = await route_inbound_message(
        db_session,
        platform="feishu",
        chat_id="oc_new",
        thread_id="om_root",
        body="@bot 帮我查一下",
        mentioned_bot=True,
        start_run=start_run,
        conversation_busy=conversation_busy,
    )
    assert decision.action == "started"
    await db_session.commit()

    # 同一条 thread 的第二条消息必须落到同一个会话，不能再开一个。
    bound = await get_thread_session(
        db_session, target=format_target("feishu", "oc_new", "om_root")
    )
    assert bound is not None and bound.conversation_id == decision.conversation_ids[0]

    again = await route_inbound_message(
        db_session,
        platform="feishu",
        chat_id="oc_new",
        thread_id="om_root",
        body="@bot 再看一下",
        mentioned_bot=True,
        start_run=start_run,
        conversation_busy=conversation_busy,
    )
    assert again.action == "steered"
    assert again.conversation_ids == decision.conversation_ids


@pytest.mark.integration
async def test_a_busy_conversation_is_steered_instead_of_started_again(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(
        db_session, title="Busy"
    )
    await subscribe_channel(
        db_session, conversation_id=conversation_id, platform="feishu", chat_id="oc_busy"
    )
    await db_session.commit()
    active = await _seed_run(db_session, conversation_id=conversation_id)

    started: list[UUID] = []

    async def start_run(*, conversation_id: UUID, goal: str) -> UUID:
        started.append(conversation_id)
        return uuid4()

    async def conversation_busy(*, conversation_id: UUID) -> UUID | None:
        return active

    await route_inbound_message(
        db_session,
        platform="feishu",
        chat_id="oc_busy",
        thread_id=None,
        body="补一句",
        mentioned_bot=False,
        start_run=start_run,
        conversation_busy=conversation_busy,
    )
    await db_session.commit()
    assert started == []


@pytest.mark.integration
async def test_a_message_with_nowhere_to_go_becomes_a_dead_letter(
    db_session: AsyncSession,
) -> None:
    """静默丢掉的话，用户看到的就是"我说了一句什么都没发生"，而且查不到为什么。"""

    async def start_run(*, conversation_id: UUID, goal: str) -> UUID:  # pragma: no cover
        raise AssertionError("不该起 run")

    async def conversation_busy(*, conversation_id: UUID) -> UUID | None:
        return None

    decision = await route_inbound_message(
        db_session,
        platform="feishu",
        chat_id="oc_nobody",
        thread_id="om_1",
        body="有人吗",
        mentioned_bot=False,
        start_run=start_run,
        conversation_busy=conversation_busy,
    )
    await db_session.commit()
    assert decision.action == "unrouted"
    entries = await list_unrouted(db_session)
    assert entries and entries[0].chat_id == "oc_nobody"
    assert entries[0].kind == "inbound"


@pytest.mark.integration
async def test_unsubscribing_stops_delivery(db_session: AsyncSession) -> None:
    conversation_id = await ensure_conversation(
        db_session, title="Unsub"
    )
    subscription = await subscribe_channel(
        db_session, conversation_id=conversation_id, platform="feishu", chat_id="oc_x"
    )
    await db_session.commit()
    assert await unsubscribe_channel(
        db_session, conversation_id=conversation_id, subscription_id=subscription.id
    )
    await db_session.commit()
    assert (
        await list_channel_subscriptions(db_session, channel=("feishu", "oc_x"))
    ) == []


async def _seed_run(session: AsyncSession, *, conversation_id: UUID) -> UUID:
    from app.runstore.runs import create_run

    run = await create_run(
        session,
        conversation_id=conversation_id,
        goal="busy",
        workflow_type="cowork",
        budget_tokens=1000,
        budget_calls=5,
        budget_wall_ms=10_000,
    )
    await session.commit()
    return run.id
