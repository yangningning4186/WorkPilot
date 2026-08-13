from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

Role = Literal["system", "user", "assistant"]


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


class AuditSink(Protocol):
    async def record(self, call: AuditRecord) -> None: ...
