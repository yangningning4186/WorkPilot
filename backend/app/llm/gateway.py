from collections.abc import AsyncIterator
from time import monotonic

import structlog

from app.core.config import Settings
from app.llm.providers.openai_compatible import OpenAICompatibleProvider
from app.llm.types import (
    AuditRecord,
    AuditSink,
    CompletionResult,
    EmbeddingResult,
    Message,
    ModelProvider,
    Usage,
)


class EmbeddingDimensionError(ValueError):
    pass


class EmbeddingIdentityError(ValueError):
    pass


class ModelGateway:
    """业务层唯一允许依赖的模型接口。"""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        embedding_dimensions: int,
        embedding_revision: str = "unversioned",
        embedding_provider: ModelProvider | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._chat_provider = provider
        self._embedding_provider = embedding_provider or provider
        self.embedding_dimensions = embedding_dimensions
        self.embedding_model = self._embedding_provider.embedding_model
        self.embedding_provider = self._embedding_provider.name
        self.embedding_revision = embedding_revision
        self._audit_sink = audit_sink

    async def complete(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult:
        started = monotonic()
        try:
            result = await self._chat_provider.complete(
                messages, max_tokens=max_tokens, temperature=temperature
            )
        except Exception:
            await self._audit(
                task_type=task_type,
                model=self._chat_provider.chat_model,
                provider=self._chat_provider,
                usage=Usage(),
                started=started,
                success=False,
            )
            raise
        await self._audit(
            task_type=task_type,
            model=result.model,
            provider=self._chat_provider,
            usage=result.usage,
            started=started,
            success=True,
        )
        return result

    def stream(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> AsyncIterator[str]:
        return self._stream(
            messages,
            task_type=task_type,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def embed(self, texts: list[str], *, task_type: str = "embedding") -> EmbeddingResult:
        if not texts or any(not text.strip() for text in texts):
            raise ValueError("embedding 输入不能为空")
        started = monotonic()
        try:
            result = await self._embedding_provider.embed(texts)
        except Exception:
            await self._audit(
                task_type=task_type,
                model=self._embedding_provider.embedding_model,
                provider=self._embedding_provider,
                usage=Usage(),
                started=started,
                success=False,
            )
            raise
        if result.model != self.embedding_model or result.provider != self.embedding_provider:
            await self._audit(
                task_type=task_type,
                model=result.model,
                provider=self._embedding_provider,
                usage=result.usage,
                started=started,
                success=False,
            )
            raise EmbeddingIdentityError(
                "embedding 响应身份与配置不一致: "
                f"期望 {self.embedding_provider}/{self.embedding_model}, "
                f"实际 {result.provider}/{result.model}"
            )
        invalid = [
            len(vector) for vector in result.embeddings if len(vector) != self.embedding_dimensions
        ]
        if invalid:
            await self._audit(
                task_type=task_type,
                model=result.model,
                provider=self._embedding_provider,
                usage=result.usage,
                started=started,
                success=False,
            )
            raise EmbeddingDimensionError(
                f"期望 {self.embedding_dimensions} 维 embedding, 实际为 {invalid[0]} 维"
            )
        await self._audit(
            task_type=task_type,
            model=result.model,
            provider=self._embedding_provider,
            usage=result.usage,
            started=started,
            success=True,
        )
        return result

    async def _stream(
        self,
        messages: list[Message],
        *,
        task_type: str,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        started = monotonic()
        success = False
        try:
            async for chunk in self._chat_provider.stream(
                messages, max_tokens=max_tokens, temperature=temperature
            ):
                yield chunk
            success = True
        finally:
            await self._audit(
                task_type=task_type,
                model=self._chat_provider.chat_model,
                provider=self._chat_provider,
                usage=Usage(),
                started=started,
                success=success,
            )

    async def _audit(
        self,
        *,
        task_type: str,
        model: str,
        provider: ModelProvider,
        usage: Usage,
        started: float,
        success: bool,
    ) -> None:
        if self._audit_sink is None:
            return
        trace_id = str(structlog.contextvars.get_contextvars().get("trace_id") or "local")
        await self._audit_sink.record(
            AuditRecord(
                trace_id=trace_id,
                task_type=task_type,
                tier="main",
                model=model,
                provider=provider.name,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                latency_ms=max(0, round((monotonic() - started) * 1000)),
                success=success,
            )
        )

    async def aclose(self) -> None:
        await self._chat_provider.aclose()
        if self._embedding_provider is not self._chat_provider:
            await self._embedding_provider.aclose()


def build_model_gateway(settings: Settings, *, audit_sink: AuditSink | None = None) -> ModelGateway:
    chat_provider = OpenAICompatibleProvider(
        base_url=settings.tier_main_base_url,
        api_key=settings.cluster_api_key,
        chat_model=settings.tier_main_model,
        embedding_model=settings.embedding_model,
        enable_thinking=settings.tier_main_enable_thinking,
        timeout_s=settings.model_timeout_s,
        trust_env=settings.model_trust_env,
    )
    embedding_provider = OpenAICompatibleProvider(
        base_url=settings.embedding_base_url or settings.tier_main_base_url,
        api_key=settings.cluster_api_key,
        chat_model=settings.tier_main_model,
        embedding_model=settings.embedding_model,
        timeout_s=settings.model_timeout_s,
        trust_env=settings.model_trust_env,
    )
    return ModelGateway(
        chat_provider,
        embedding_dimensions=settings.embedding_dim,
        embedding_revision=settings.embedding_revision,
        embedding_provider=embedding_provider,
        audit_sink=audit_sink,
    )
