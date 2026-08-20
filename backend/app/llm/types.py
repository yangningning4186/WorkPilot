from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import UUID

from app.llm.errors import ProviderNotDispatchedError as ProviderNotDispatchedError

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


@dataclass(frozen=True)
class AuditRecord:
    trace_id: str
    task_type: str
    tier: Literal["light", "main", "heavy", "external"]
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    success: bool
    prompt_cache_read_tokens: int = 0
    prompt_cache_write_tokens: int = 0
    cost_usd: Decimal | None = None
    # 缓存命中与 fallback 都是"这次调用实际发生了什么"的一部分。列在 M0 就建好了,
    # 一直没人写——看板要算命中率与降级率(docs/07 §9)就得靠这三列。
    cached: bool = False
    cache_type: Literal["exact", "semantic", "prompt"] | None = None
    was_fallback: bool = False
    run_id: UUID | None = None
    # 评测跑批的归属。与 run_id 是两张表的外键(agent_runs / eval_runs), 不能混用:
    # 评测跑批的逐条 token 与成本就是靠它归集的。
    eval_run_id: UUID | None = None
    # 同批并发调用共享一次 GPU 计时(docs/07 §7.2)。线上单条问答不是批次, 保持 NULL。
    batch_id: UUID | None = None


class AuditSink(Protocol):
    async def record(self, call: AuditRecord) -> None: ...


class BudgetGuard(Protocol):
    """调用前原子预留、调用后结算的费用闸门(docs/12 §2.2)。

    实现必须使用独立于业务事务的连接: 钱花出去了就是花出去了, 不能随业务回滚一起消失。
    """

    async def reserve(
        self,
        *,
        idempotency_key: str,
        estimated_usd: Decimal,
        run_id: UUID | None = None,
    ) -> None:
        """预留失败时抛异常, 调用方不得再发起模型调用。"""
        ...

    async def settle(self, *, idempotency_key: str, actual_usd: Decimal) -> None: ...

    async def release_undispatched(self, *, idempotency_key: str) -> None: ...
