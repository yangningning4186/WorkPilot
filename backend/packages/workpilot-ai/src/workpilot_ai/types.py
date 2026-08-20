from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from workpilot_ai.errors import ProviderNotDispatchedError as ProviderNotDispatchedError
from workpilot_telemetry.budget import BudgetGuard as BudgetGuard
from workpilot_telemetry.records import AuditRecord as AuditRecord
from workpilot_telemetry.records import AuditSink as AuditSink

Role = Literal["system", "user", "assistant", "tool"]
AttachmentKind = Literal["image", "pdf", "text"]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    strict: bool = True


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
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult: ...

    def stream(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
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
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult: ...


class PromptCachingToolCallingProvider(Protocol):
    """支持显式 Provider Prompt Cache 控制的 tool-calling adapter。"""

    async def complete_with_tools_prompt_cache(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        max_tokens: int,
        temperature: float,
        prompt_cache_key: str,
    ) -> CompletionResult: ...
