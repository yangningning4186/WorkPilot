"""频道与 thread 的地址串。

一个字符串同时服务三处：查订阅、投递消息、以及判定"这条 thread 已经被授权可以直接回"。
三处各写一套格式，迟早会出现"看起来一样但比不相等"的地址——那种 bug 表现为消息发出去了
却没人收到，或者授权明明给了却还在弹审批。

格式：``<platform>:<chat_id>``，thread 再追加 ``:<thread_id>``。
chat_id 与 thread_id 不允许包含冒号，解析因此是无歧义的。
"""

from __future__ import annotations

from app.cowork_contracts import MessagingPlatform

SUPPORTED_PLATFORMS: frozenset[str] = frozenset({"feishu"})


class TargetError(ValueError):
    """面向用户与模型的地址错误。"""


def _validate_segment(value: str, *, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise TargetError(f"{field} 不能为空")
    if ":" in normalized:
        raise TargetError(f"{field} 不能包含冒号，否则地址串无法无歧义解析")
    if len(normalized) > 200:
        raise TargetError(f"{field} 不能超过 200 个字符")
    return normalized


def format_target(
    platform: MessagingPlatform, chat_id: str, thread_id: str | None = None
) -> str:
    chat = _validate_segment(chat_id, field="chat_id")
    if thread_id is None:
        return f"{platform}:{chat}"
    return f"{platform}:{chat}:{_validate_segment(thread_id, field='thread_id')}"


def parse_target(target: str) -> tuple[MessagingPlatform, str, str | None]:
    parts = target.split(":")
    if len(parts) not in (2, 3):
        raise TargetError(f"地址串格式应为 platform:chat_id[:thread_id]，收到 {target!r}")
    platform, chat_id, *rest = parts
    if platform not in SUPPORTED_PLATFORMS:
        raise TargetError(f"暂不支持的消息平台 {platform!r}")
    return (
        platform,  # type: ignore[return-value]
        _validate_segment(chat_id, field="chat_id"),
        _validate_segment(rest[0], field="thread_id") if rest else None,
    )
