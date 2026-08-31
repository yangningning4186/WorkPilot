"""Agent-owned message layer and the single provider conversion boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import escape
from typing import Any, Literal, NotRequired, TypedDict, cast

from workpilot_ai.types import (
    Message,
    MessageAttachment,
    RedactedThinkingContentBlock,
    TextContentBlock,
    ThinkingContentBlock,
    ToolCall,
    content_blocks_from_payload,
)


class CanonicalToolFunction(TypedDict):
    name: str
    arguments: str


class CanonicalToolCall(TypedDict):
    id: str
    type: Literal["function"]
    function: CanonicalToolFunction
    thought_signature: NotRequired[str]


AgentMessageRole = Literal[
    "system",
    "user",
    "assistant",
    "tool",
    "compaction_summary",
    "runtime_directive",
    "custom",
]


class AgentMessage(TypedDict, total=False):
    """Canonical harness message, including records that are not provider messages."""

    role: AgentMessageRole
    content: str
    tool_calls: list[CanonicalToolCall]
    tool_call_id: str
    attachments: list[dict[str, Any]]
    content_blocks: list[dict[str, str]]
    created_at: str
    stop_reason: Literal["stop", "length", "tool_use", "error"]
    # runtime_directive provenance, or a custom message discriminator.
    source: str
    kind: str
    # Custom messages are UI-only by default. Explicit visibility is fail-closed.
    llm_visible: bool


def runtime_directive(content: str, *, source: str) -> AgentMessage:
    if not source.strip():
        raise ValueError("runtime directive source 不能为空")
    return {
        "role": "runtime_directive",
        "content": content,
        "source": source.strip(),
    }


def compaction_summary(content: str) -> AgentMessage:
    return {"role": "compaction_summary", "content": content}


def _message_attachments(raw: Mapping[str, Any]) -> tuple[MessageAttachment, ...]:
    attachments = raw.get("attachments", [])
    if not isinstance(attachments, list):
        raise ValueError("AgentMessage attachments 必须是数组")
    return tuple(
        MessageAttachment(
            kind=cast("Any", item["kind"]),
            filename=str(item["filename"]),
            media_type=str(item["media_type"]),
            path=str(item["path"]),
            size_bytes=int(item["size_bytes"]),
            sha256=str(item["sha256"]),
            extracted_text=str(item.get("extracted_text", "")),
        )
        for item in attachments
        if isinstance(item, Mapping)
    )


def _message_content_blocks(
    raw: Mapping[str, Any],
) -> tuple[TextContentBlock | ThinkingContentBlock | RedactedThinkingContentBlock, ...]:
    return content_blocks_from_payload(raw.get("content_blocks", []))


def convert_to_llm(raw: Mapping[str, Any]) -> Message | None:
    """Flatten one AgentMessage for a provider; UI-only records return ``None``."""

    role = raw.get("role")
    content = str(raw.get("content", ""))
    if role == "runtime_directive":
        source = escape(str(raw.get("source") or "runtime"), quote=True)
        return Message(
            role="user",
            content=(f'<runtime_directive source="{source}">\n{content}\n</runtime_directive>'),
            attachments=_message_attachments(raw),
            source=str(raw.get("source") or "runtime"),
        )
    if role == "compaction_summary":
        return Message(role="user", content=content)
    if role == "custom":
        if raw.get("llm_visible") is not True:
            return None
        return Message(role="user", content=content)
    if role not in {"system", "user", "assistant", "tool"}:
        raise ValueError(f"非法 AgentMessage role: {role!r}")

    calls: list[ToolCall] = []
    raw_calls = raw.get("tool_calls", [])
    if isinstance(raw_calls, list):
        for item in raw_calls:
            if not isinstance(item, Mapping) or not isinstance(item.get("function"), Mapping):
                raise ValueError("非法 canonical tool_call")
            function = cast("Mapping[str, Any]", item["function"])
            thought_signature = item.get("thought_signature", "")
            if not isinstance(thought_signature, str):
                raise ValueError("canonical tool_call thought_signature 必须是字符串")
            calls.append(
                ToolCall(
                    id=str(item.get("id", "")),
                    name=str(function.get("name", "")),
                    arguments=str(function.get("arguments", "")),
                    thought_signature=thought_signature,
                )
            )
    return Message(
        role=cast("Literal['system', 'user', 'assistant', 'tool']", role),
        content=content,
        tool_calls=tuple(calls),
        tool_call_id=(None if raw.get("tool_call_id") is None else str(raw["tool_call_id"])),
        attachments=_message_attachments(raw),
        source=(None if raw.get("source") is None else str(raw["source"])),
        content_blocks=_message_content_blocks(raw),
    )


def convert_to_llm_messages(messages: Sequence[Mapping[str, Any]]) -> list[Message]:
    converted: list[Message] = []
    for message in messages:
        provider_message = convert_to_llm(message)
        if provider_message is not None:
            converted.append(provider_message)
    return converted
