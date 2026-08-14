from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from time import monotonic
from uuid import UUID

import structlog
from uuid6 import uuid7

from app.core.config import Settings
from app.llm.pricing import GatewayPricing, ModelPricing, estimate_tokens, is_measured
from app.llm.providers.openai_compatible import OpenAICompatibleProvider
from app.llm.types import (
    AuditRecord,
    AuditSink,
    BudgetGuard,
    CompletionResult,
    EmbeddingResult,
    Message,
    ModelProvider,
    ProviderNotDispatchedError,
    Usage,
)

logger = structlog.get_logger(__name__)


class EmbeddingDimensionError(ValueError):
    pass


class EmbeddingIdentityError(ValueError):
    pass


@dataclass
class _Reservation:
    """一次模型调用占用的额度。

    幂等键每次调用现生成: 网关只负责"这次真的发出去的调用有没有超预算", 重放去重
    是上层 tool_invocations 的职责(ADR-0007), 在这里做会把两种语义混在一起。
    """

    guard: BudgetGuard | None
    idempotency_key: str
    estimated_usd: Decimal

    async def settle(self, actual_usd: Decimal) -> Decimal:
        charged = min(actual_usd, self.estimated_usd)
        if charged < actual_usd:
            logger.warning(
                "模型实际用量超出预留上限, 按上限结算",
                estimated_usd=str(self.estimated_usd),
                actual_usd=str(actual_usd),
            )
        if self.guard is not None:
            await self.guard.settle(idempotency_key=self.idempotency_key, actual_usd=charged)
        return charged

    async def release(self) -> None:
        if self.guard is not None:
            await self.guard.release_undispatched(idempotency_key=self.idempotency_key)

    async def abandon(self) -> None:
        """是否已计费不明: 保留预留, 到期后由 sweeper 按上限记账。"""

        if self.guard is not None:
            logger.warning(
                "模型调用结果不明, 预留保留至到期后按上限记账",
                idempotency_key=self.idempotency_key,
                estimated_usd=str(self.estimated_usd),
            )


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
        budget_guard: BudgetGuard | None = None,
        pricing: GatewayPricing | None = None,
        chars_per_token: float = 1.0,
        run_id: UUID | None = None,
    ) -> None:
        self._chat_provider = provider
        self._embedding_provider = embedding_provider or provider
        self.embedding_dimensions = embedding_dimensions
        self.embedding_model = self._embedding_provider.embedding_model
        self.embedding_provider = self._embedding_provider.name
        self.embedding_revision = embedding_revision
        self._audit_sink = audit_sink
        self._budget_guard = budget_guard
        self._pricing = pricing or GatewayPricing()
        self._chars_per_token = chars_per_token
        self._run_id = run_id

    async def complete(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> CompletionResult:
        estimated_usage = Usage(
            input_tokens=self._estimate_tokens(sum(len(item.content) for item in messages)),
            output_tokens=max_tokens,
        )
        reservation = await self._reserve(self._pricing.chat, estimated_usage)
        started = monotonic()
        try:
            result = await self._chat_provider.complete(
                messages, max_tokens=max_tokens, temperature=temperature
            )
        except ProviderNotDispatchedError:
            await reservation.release()
            await self._audit(
                task_type=task_type,
                model=self._chat_provider.chat_model,
                provider=self._chat_provider,
                usage=Usage(),
                started=started,
                success=False,
                cost_usd=Decimal(0),
            )
            raise
        except Exception:
            await reservation.abandon()
            await self._audit(
                task_type=task_type,
                model=self._chat_provider.chat_model,
                provider=self._chat_provider,
                usage=Usage(),
                started=started,
                success=False,
                cost_usd=reservation.estimated_usd,
            )
            raise
        charged = await self._settle(reservation, self._pricing.chat, result.usage)
        await self._audit(
            task_type=task_type,
            model=result.model,
            provider=self._chat_provider,
            usage=result.usage,
            started=started,
            success=True,
            cost_usd=charged,
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
        estimated_usage = Usage(
            input_tokens=self._estimate_tokens(sum(len(text) for text in texts))
        )
        reservation = await self._reserve(self._pricing.embedding, estimated_usage)
        started = monotonic()
        try:
            result = await self._embedding_provider.embed(texts)
        except ProviderNotDispatchedError:
            await reservation.release()
            await self._audit(
                task_type=task_type,
                model=self._embedding_provider.embedding_model,
                provider=self._embedding_provider,
                usage=Usage(),
                started=started,
                success=False,
                cost_usd=Decimal(0),
            )
            raise
        except Exception:
            await reservation.abandon()
            await self._audit(
                task_type=task_type,
                model=self._embedding_provider.embedding_model,
                provider=self._embedding_provider,
                usage=Usage(),
                started=started,
                success=False,
                cost_usd=reservation.estimated_usd,
            )
            raise
        # 身份与维度校验失败同样是"已经花了钱"的成功调用, 先结算再抛错。
        charged = await self._settle(reservation, self._pricing.embedding, result.usage)
        identity_mismatch = (
            result.model != self.embedding_model or result.provider != self.embedding_provider
        )
        invalid_dims = [
            len(vector) for vector in result.embeddings if len(vector) != self.embedding_dimensions
        ]
        await self._audit(
            task_type=task_type,
            model=result.model,
            provider=self._embedding_provider,
            usage=result.usage,
            started=started,
            success=not (identity_mismatch or invalid_dims),
            cost_usd=charged,
        )
        if identity_mismatch:
            raise EmbeddingIdentityError(
                "embedding 响应身份与配置不一致: "
                f"期望 {self.embedding_provider}/{self.embedding_model}, "
                f"实际 {result.provider}/{result.model}"
            )
        if invalid_dims:
            raise EmbeddingDimensionError(
                f"期望 {self.embedding_dimensions} 维 embedding, 实际为 {invalid_dims[0]} 维"
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
        estimated_usage = Usage(
            input_tokens=self._estimate_tokens(sum(len(item.content) for item in messages)),
            output_tokens=max_tokens,
        )
        reservation = await self._reserve(self._pricing.chat, estimated_usage)
        started = monotonic()
        produced_chars = 0
        success = False
        dispatched = True
        try:
            async for chunk in self._chat_provider.stream(
                messages, max_tokens=max_tokens, temperature=temperature
            ):
                produced_chars += len(chunk)
                yield chunk
            success = True
        except ProviderNotDispatchedError:
            dispatched = False
            raise
        finally:
            # 流式接口拿不到 provider 回报的用量, 按已产出文本估算并受预留上限约束。
            usage = Usage(
                input_tokens=estimated_usage.input_tokens,
                output_tokens=min(self._estimate_tokens(produced_chars), max_tokens),
            )
            if not dispatched:
                await reservation.release()
                cost_usd = Decimal(0)
            elif success:
                cost_usd = await reservation.settle(self._pricing.chat.cost_usd(usage))
            else:
                await reservation.abandon()
                cost_usd = reservation.estimated_usd
            await self._audit(
                task_type=task_type,
                model=self._chat_provider.chat_model,
                provider=self._chat_provider,
                usage=usage if success else Usage(),
                started=started,
                success=success,
                cost_usd=cost_usd,
            )

    async def _reserve(self, pricing: ModelPricing, estimated_usage: Usage) -> _Reservation:
        estimated_usd = pricing.cost_usd(estimated_usage)
        if self._budget_guard is None or estimated_usd == 0:
            # 本地自部署模型价格为 0, 走预留只是白白多两次数据库往返。
            return _Reservation(guard=None, idempotency_key="", estimated_usd=estimated_usd)
        reservation = _Reservation(
            guard=self._budget_guard,
            idempotency_key=f"llm:{uuid7()}",
            estimated_usd=estimated_usd,
        )
        await self._budget_guard.reserve(
            idempotency_key=reservation.idempotency_key,
            estimated_usd=estimated_usd,
            run_id=self._run_id,
        )
        return reservation

    async def _settle(
        self, reservation: _Reservation, pricing: ModelPricing, usage: Usage
    ) -> Decimal:
        # provider 没回报用量时不能当 0 计费, 按预留上限保守结算。
        actual_usd = pricing.cost_usd(usage) if is_measured(usage) else reservation.estimated_usd
        return await reservation.settle(actual_usd)

    def _estimate_tokens(self, chars: int) -> int:
        return estimate_tokens(chars, chars_per_token=self._chars_per_token)

    async def _audit(
        self,
        *,
        task_type: str,
        model: str,
        provider: ModelProvider,
        usage: Usage,
        started: float,
        success: bool,
        cost_usd: Decimal | None = None,
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
                cost_usd=cost_usd,
                run_id=self._run_id,
            )
        )

    async def aclose(self) -> None:
        await self._chat_provider.aclose()
        if self._embedding_provider is not self._chat_provider:
            await self._embedding_provider.aclose()


def gateway_pricing_from_settings(settings: Settings) -> GatewayPricing:
    return GatewayPricing(
        chat=ModelPricing(
            input_usd_per_mtok=settings.price_main_input_usd_per_mtok,
            output_usd_per_mtok=settings.price_main_output_usd_per_mtok,
        ),
        embedding=ModelPricing(
            input_usd_per_mtok=settings.price_embedding_input_usd_per_mtok,
        ),
    )


def build_model_gateway(
    settings: Settings,
    *,
    audit_sink: AuditSink | None = None,
    budget_guard: BudgetGuard | None = None,
    run_id: UUID | None = None,
) -> ModelGateway:
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
        budget_guard=budget_guard,
        pricing=gateway_pricing_from_settings(settings),
        chars_per_token=settings.cost_estimate_chars_per_token,
        run_id=run_id,
    )
