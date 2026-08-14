from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

Role = Literal["system", "user", "assistant"]


class ProviderNotDispatchedError(RuntimeError):
    """确认请求尚未发给 provider。

    只有能证明"一个字节都没发出去"的错误才允许抛这个类型: 它是费用释放的唯一依据。
    发出之后的任何失败(读超时、连接中断)都可能已经计费, 必须走保守记账路径。
    """


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class CompletionResult:
    text: str
    model: str
    provider: str
    usage: Usage = field(default_factory=Usage)


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
    cost_usd: Decimal | None = None
    run_id: UUID | None = None


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
