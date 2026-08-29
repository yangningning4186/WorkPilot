from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from workpilot_ai.errors import ProviderNotDispatchedError as ProviderNotDispatchedError
from workpilot_telemetry.budget import BudgetGuard as BudgetGuard
from workpilot_telemetry.records import AuditRecord as AuditRecord
from workpilot_telemetry.records import AuditSink as AuditSink

Role = Literal["system", "user", "assistant", "tool"]
AttachmentKind = Literal["image", "pdf", "text"]
CompletionStopReason = Literal["stop", "length", "tool_use", "error"]
CacheRetention = Literal["default", "none"]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    strict: bool = True
    prompt_snippet: str = ""
    prompt_guidelines: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class MessageAttachment:
    """Provider-neutral input attachment stored outside canonical message bodies."""

    kind: AttachmentKind
    filename: str
    media_type: str
    path: str
    size_bytes: int
    sha256: str
    extracted_text: str = ""


@dataclass(frozen=True)
class TextContentBlock:
    """Provider-neutral assistant text block.

    ``Message.content`` remains as the compatibility/display projection used by providers
    whose wire format is plain text.  ``content_blocks`` is the lossless protocol history.
    """

    text: str
    type: Literal["text"] = field(default="text", init=False)


@dataclass(frozen=True)
class ThinkingContentBlock:
    """Anthropic extended-thinking block that must be replayed with its signature."""

    thinking: str
    signature: str
    type: Literal["thinking"] = field(default="thinking", init=False)


@dataclass(frozen=True)
class RedactedThinkingContentBlock:
    """Opaque Anthropic redacted-thinking block; ``data`` must remain byte-for-byte stable."""

    data: str
    type: Literal["redacted_thinking"] = field(default="redacted_thinking", init=False)


type MessageContentBlock = TextContentBlock | ThinkingContentBlock | RedactedThinkingContentBlock


def content_block_payload(block: MessageContentBlock) -> dict[str, str]:
    if isinstance(block, TextContentBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ThinkingContentBlock):
        return {
            "type": "thinking",
            "thinking": block.thinking,
            "signature": block.signature,
        }
    return {"type": "redacted_thinking", "data": block.data}


def content_blocks_from_payload(raw: object) -> tuple[MessageContentBlock, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list | tuple):
        raise ValueError("content_blocks 必须是数组")
    converted: list[MessageContentBlock] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("content block 必须是 object")
        kind = item.get("type")
        if kind == "text" and isinstance(item.get("text"), str):
            converted.append(TextContentBlock(text=str(item["text"])))
        elif (
            kind == "thinking"
            and isinstance(item.get("thinking"), str)
            and isinstance(item.get("signature"), str)
            and item.get("signature")
        ):
            converted.append(
                ThinkingContentBlock(
                    thinking=str(item["thinking"]), signature=str(item["signature"])
                )
            )
        elif kind == "redacted_thinking" and isinstance(item.get("data"), str) and item.get("data"):
            converted.append(RedactedThinkingContentBlock(data=str(item["data"])))
        else:
            raise ValueError(f"非法 content block: {kind!r}")
    return tuple(converted)


@dataclass(frozen=True)
class Message:
    """Provider-neutral canonical chat message.

    Tool history deliberately mirrors the OpenAI message contract: the assistant owns
    ``tool_calls`` and every tool result points back with ``tool_call_id``.
    """

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    attachments: tuple[MessageAttachment, ...] = ()
    # Lossless provider protocol blocks.  Other adapters may keep using ``content``; Anthropic
    # uses this tuple to replay signed thinking across tool turns.
    content_blocks: tuple[MessageContentBlock, ...] = ()


@dataclass(frozen=True)
class Usage:
    """Provider-neutral token usage。

    ``input_tokens`` 始终是完整输入量；prompt cache read/write 是其子集，用于衡量
    Provider 侧前缀缓存，不等同于 WorkPilot 的 Redis 精确结果缓存。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    prompt_cache_read_tokens: int = 0
    prompt_cache_write_tokens: int = 0


@dataclass(frozen=True)
class CompletionResult:
    text: str
    model: str
    provider: str
    usage: Usage = field(default_factory=Usage)
    tool_calls: tuple[ToolCall, ...] = ()
    # Provider adapters must preserve why generation stopped.  A syntactically valid partial
    # tool call or non-empty half answer is still unsafe when the provider hit its output cap.
    stop_reason: CompletionStopReason = "stop"
    # Stable configured endpoint identity. ``model`` may be a provider-returned alias, while
    # resume compatibility must compare the route that was actually selected.
    model_identity: str | None = None
    # Ordered assistant protocol blocks returned by providers that require lossless replay.
    content_blocks: tuple[MessageContentBlock, ...] = ()


@dataclass(frozen=True)
class ToolCallDelta:
    """One provider tool-call protocol fragment.

    The raw argument fragment is intentionally kept at the provider boundary.  Product layers
    may use its length for progress, but must not persist it: tool arguments can contain file
    bodies, connector payloads, or credentials.
    """

    index: int
    id: str = ""
    name_delta: str = ""
    arguments_delta: str = ""


@dataclass(frozen=True)
class CompletionChunk:
    """流式 tool-calling 的一块。

    四种块**互斥地**承载正文增量、思考增量、tool-call 协议增量，以及最后一块的完整
    ``CompletionResult``。终块的存在是刻意的——工具调用的参数是逐片拼出来的，只有
    收完才谈得上"这一轮模型决定调哪几只工具"，而调用方需要的正是那个整体。有了终块，
    调用方可以把流当成"complete_with_tools + 一路上的 delta"，决策逻辑一行都不用改。

    ``reasoning_delta`` 与 ``text_delta`` 分开而不是拼在一起：思考过程不进 canonical
    历史、不参与引用、也不该被当成回答的一部分落盘。混成一路之后再想分开就只能靠
    猜标记，那是一类必然出错的解析。
    """

    text_delta: str = ""
    reasoning_delta: str = ""
    tool_call_delta: ToolCallDelta | None = None
    result: CompletionResult | None = None


@dataclass(frozen=True)
class EmbeddingResult:
    embeddings: list[list[float]]
    model: str
    provider: str
    usage: Usage = field(default_factory=Usage)


class ModelProvider(Protocol):
    name: str
    chat_model: str
    embedding_model: str

    async def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int | None,
        temperature: float,
    ) -> CompletionResult: ...

    def stream(
        self,
        messages: list[Message],
        *,
        max_tokens: int | None,
        temperature: float,
    ) -> AsyncIterator[str]: ...

    async def embed(self, texts: list[str]) -> EmbeddingResult: ...

    async def aclose(self) -> None: ...


class ToolCallingProvider(Protocol):
    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int | None,
        temperature: float,
    ) -> CompletionResult: ...


class StreamingToolCallingProvider(Protocol):
    """真正按 SSE 流式返回、且能在流里累出 tool_call 的 adapter。

    单独一个 Protocol 而不是往 ``ToolCallingProvider`` 上加方法：不是每个 Provider 都
    做得到（Gemini 的会话适配器现在就没有），而"做不到"必须是网关能检测并优雅降级的
    情况，不能变成运行时 AttributeError。
    """

    def stream_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int | None,
        temperature: float,
    ) -> AsyncIterator[CompletionChunk]: ...


class PromptCachingToolCallingProvider(Protocol):
    """支持显式 Provider Prompt Cache 控制的 tool-calling adapter。"""

    async def complete_with_tools_prompt_cache(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int | None,
        temperature: float,
        prompt_cache_key: str,
    ) -> CompletionResult: ...
