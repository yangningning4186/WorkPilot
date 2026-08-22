"""聊天平台上的按钮式审批。

自由文本回复做审批有两个具体问题：要在回复里带一个 ``[wp:id]`` 标记（用户会删掉、
客户端会截断），以及要靠 thread 关系反查是哪条请求（转发一次就断了）。按钮不需要这些
——item id 就编在按钮的 value 里，点击回来的那一下自带身份。

这一层是**平台无关**的：`Button` 只是 `(label, value)`，各家适配器自己渲染
（飞书 interactive card、Slack Block Kit、Telegram inline keyboard）。value 对适配器
不透明，它的含义只由这里的 `encode` / `decode` 定义。

自由文本的答复（`ask_user` 的开放问题）不在聊天里做：那种要打字的场景请回应用里。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from uuid import UUID

# 各家平台对按钮 value 都有长度限制（飞书 1KB 量级）。编码后超过这个数就说明我们
# 往里塞了不该塞的东西。
MAX_VALUE_BYTES = 512


@dataclass(frozen=True)
class Button:
    label: str
    value: str


class ButtonError(ValueError):
    pass


def encode(item_id: UUID, resolution: str) -> str:
    payload = json.dumps(
        {"i": str(item_id), "r": resolution}, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    # base64url 而不是裸 JSON：几家平台的 value 字段对引号和花括号的转义各不相同，
    # 编码成纯 ASCII 之后就不必逐个平台去猜谁会改写它。
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    if len(encoded) > MAX_VALUE_BYTES:
        raise ButtonError("按钮 value 超长")
    return encoded


def decode(value: str) -> tuple[UUID, str]:
    padding = "=" * (-len(value) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(value + padding))
        return UUID(str(payload["i"])), str(payload["r"])
    except (KeyError, ValueError, TypeError) as error:
        raise ButtonError(f"无法解析按钮 value：{error}") from error


def approval_buttons(item_id: UUID) -> tuple[Button, ...]:
    return (
        Button(label="批准", value=encode(item_id, "approve")),
        Button(label="拒绝", value=encode(item_id, "reject")),
    )


def choice_buttons(item_id: UUID, choices: list[str]) -> tuple[Button, ...]:
    # 选项也走同一条 (item_id, resolution) 通道：resolution 就是选项文本本身。
    # 超长的选项直接不给按钮——截断后的按钮点下去，用户以为自己选的是另一件事。
    buttons: list[Button] = []
    for choice in choices[:4]:
        try:
            buttons.append(Button(label=choice[:60], value=encode(item_id, choice)))
        except ButtonError:
            continue
    return tuple(buttons)
