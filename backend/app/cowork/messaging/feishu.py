"""飞书适配器：唯一的具体传输实现。

选它是因为它已经是本项目的官方连接器（OAuth、加密令牌、固定官方主机都已就绪），
而且同时具备发交互卡片和回推事件这两件事——`routing` 需要前者，`subscriptions`
与 `mentions` 需要后者。

这里只做两件事：把 `(text, buttons)` 渲染成飞书的消息体，以及把飞书回推的事件解析成
平台无关的形状。任何"该由谁处理"的判断都不在这里，那是 `inbound` 的事。

**签名校验在解析之前。** 事件回调地址是公网可达的；不校验就等于任何人都能伪造一条
"用户批准了那条命令"。
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from app.cowork.connectors import ConnectorAccountRecord, connector_secrets
from app.cowork.messaging.buttons import Button
from app.cowork.messaging.routing import Sender
from app.cowork_contracts import MessagingPlatform
from app.security.secret_store import LocalSecretStore

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"


class FeishuError(RuntimeError):
    pass


@dataclass(frozen=True)
class InboundEvent:
    """一条已经过签名校验、且与平台无关的入站事件。"""

    kind: str  # "message" | "card_action"
    chat_id: str
    thread_id: str | None
    sender_id: str | None
    text: str
    mentioned_bot: bool
    card_value: str | None
    raw: dict[str, Any]


def verify_signature(
    *,
    timestamp: str,
    nonce: str,
    encrypt_key: str,
    body: bytes,
    signature: str,
) -> bool:
    """飞书事件签名：sha256(timestamp + nonce + encrypt_key + body)。

    用 `compare_digest` 而不是 `==`：签名比较必须是常数时间的，否则逐字节早退的耗时
    差异本身就是一条侧信道。
    """

    import hmac

    digest = hashlib.sha256(
        timestamp.encode("utf-8") + nonce.encode("utf-8") + encrypt_key.encode("utf-8") + body
    ).hexdigest()
    return hmac.compare_digest(digest, signature)


def decrypt_event(encrypted: str, encrypt_key: str) -> dict[str, Any]:
    """解开飞书的 AES-256-CBC 事件密文。"""

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    try:
        raw = base64.b64decode(encrypted, validate=True)
    except (ValueError, TypeError) as error:
        raise FeishuError("事件密文不是合法 base64") from error
    if len(raw) < 32 or len(raw) % 16 != 0:
        raise FeishuError("事件密文长度无效")
    iv, payload = raw[:16], raw[16:]
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plain = decryptor.update(payload) + decryptor.finalize()
    except ValueError as error:
        raise FeishuError("事件密文无法解密") from error
    # PKCS#7：最后一个字节就是填充长度。
    padding = plain[-1]
    if padding < 1 or padding > 16 or plain[-padding:] != bytes([padding]) * padding:
        raise FeishuError("事件密文填充无效")
    try:
        parsed = json.loads(plain[:-padding].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise FeishuError("事件密文内容无效") from error
    if not isinstance(parsed, dict):
        raise FeishuError("事件密文解开后不是 JSON object")
    return parsed


def parse_event(payload: dict[str, Any], *, bot_open_id: str | None) -> InboundEvent | None:
    """把飞书事件解析成平台无关的形状；不认识的事件返回 None。"""

    header = payload.get("header", {})
    event = payload.get("event", {})
    event_type = str(header.get("event_type") or payload.get("type") or "")

    if event_type == "im.message.receive_v1":
        message = event.get("message", {})
        chat_id = str(message.get("chat_id") or "")
        if not chat_id:
            return None
        content = message.get("content") or "{}"
        try:
            text = str(json.loads(content).get("text") or "")
        except json.JSONDecodeError:
            text = ""
        mentions = message.get("mentions") or []
        mentioned = bot_open_id is not None and any(
            str((item.get("id") or {}).get("open_id") or "") == bot_open_id for item in mentions
        )
        return InboundEvent(
            kind="message",
            chat_id=chat_id,
            # root_id 才是整条 thread 的锚；parent_id 指向被直接回复的那一条，
            # 用它做键会让同一条 thread 里的两条回复落到两个"thread"上。
            thread_id=str(message.get("root_id") or message.get("message_id") or ""),
            sender_id=str(((event.get("sender") or {}).get("sender_id") or {}).get("open_id") or "")
            or None,
            text=text,
            mentioned_bot=mentioned,
            card_value=None,
            raw=payload,
        )

    if event_type == "card.action.trigger":
        action = event.get("action", {})
        context = event.get("context", {})
        value = action.get("value")
        return InboundEvent(
            kind="card_action",
            chat_id=str(context.get("open_chat_id") or ""),
            thread_id=None,
            sender_id=str((event.get("operator") or {}).get("open_id") or "") or None,
            text="",
            mentioned_bot=True,
            card_value=value if isinstance(value, str) else json.dumps(value),
            raw=payload,
        )
    return None


def render_message(text: str, buttons: Sequence[Button]) -> tuple[str, dict[str, Any]]:
    """渲染成飞书的 `(msg_type, content)`。

    没有按钮时发纯文本：一张只有正文的卡片在手机通知里反而更难读。
    """

    if not buttons:
        return "text", {"text": text}
    card = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": button.label},
                        # value 对适配器不透明：它的含义由 buttons.encode 定义。
                        "value": button.value,
                        "type": "primary" if index == 0 else "default",
                    }
                    for index, button in enumerate(buttons)
                ],
            },
        ],
    }
    return "interactive", card


async def send_message(
    *,
    account: ConnectorAccountRecord,
    secret_store: LocalSecretStore,
    chat_id: str,
    text: str,
    buttons: Sequence[Button],
    timeout_s: float,
) -> str:
    """发一条消息，返回平台侧 message_id。"""

    secrets = connector_secrets(account, secret_store)
    access_token = str(secrets.get("access_token") or "").strip()
    if not access_token:
        raise FeishuError("飞书连接器缺少 access_token，请先完成 OAuth")
    msg_type, content = render_message(text, buttons)
    async with httpx.AsyncClient(
        timeout=timeout_s, follow_redirects=False, trust_env=False
    ) as client:
        response = await client.post(
            f"{FEISHU_API_BASE}/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                "receive_id": chat_id,
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
        )
    if response.status_code < 200 or response.status_code >= 300:
        raise FeishuError(f"飞书发送失败：HTTP {response.status_code}")
    payload = response.json()
    if payload.get("code") not in (0, None):
        raise FeishuError(f"飞书发送失败：{payload.get('msg')}")
    message_id = str(((payload.get("data") or {}).get("message_id")) or "")
    if not message_id:
        raise FeishuError("飞书没有返回 message_id")
    return message_id


def account_sender(
    *,
    account: ConnectorAccountRecord,
    secret_store: LocalSecretStore,
    timeout_s: float,
) -> Sender:
    """把一个飞书账户包装成 `routing.Sender`。"""

    async def sender(
        *,
        platform: MessagingPlatform,
        chat_id: str,
        connector_account_id: UUID | None,
        text: str,
        buttons: Sequence[Button],
    ) -> str:
        if platform != "feishu":
            raise FeishuError(f"飞书适配器不能投递到 {platform}")
        return await send_message(
            account=account,
            secret_store=secret_store,
            chat_id=chat_id,
            text=text,
            buttons=buttons,
            timeout_s=timeout_s,
        )

    return sender
