from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from time import monotonic
from typing import Protocol
from uuid import UUID

import structlog
from uuid6 import uuid7

from app.core.config import Settings
from app.llm.cache import CompletionCache, completion_cache_key, is_cacheable
from app.llm.pricing import GatewayPricing, ModelPricing, estimate_tokens, is_measured
from app.llm.providers.openai_compatible import OpenAICompatibleProvider
from app.llm.routing import (
    EndpointSpec,
    RoutingMode,
    RoutingTable,
    Tier,
    load_routing_table,
    routing_env,
)
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


class TierProviderPool:
    """按档位持有 provider 实例。

    同一档位复用同一个实例（也就是同一个 HTTP 连接池）；不同档位即使指向同一台机器
    也分开，因为 `model` 不同，而 provider 的身份是 (base_url, model) 这一对。
    """

    def __init__(
        self,
        table: RoutingTable,
        *,
        embedding_model: str,
        trust_env: bool,
        factory: "ProviderFactory | None" = None,
    ) -> None:
        self._table = table
        self._embedding_model = embedding_model
        self._trust_env = trust_env
        self._factory = factory or _default_provider_factory
        self._providers: dict[Tier, ModelProvider] = {}

    def _provider(self, endpoint: EndpointSpec) -> ModelProvider:
        cached = self._providers.get(endpoint.tier)
        if cached is None:
            cached = self._factory(
                endpoint, embedding_model=self._embedding_model, trust_env=self._trust_env
            )
            self._providers[endpoint.tier] = cached
        return cached

    def chain(
        self, task_type: str, *, mode: RoutingMode, tier_override: Tier | None = None
    ) -> tuple[tuple[Tier, ModelProvider], ...]:
        return tuple(
            (endpoint.tier, self._provider(endpoint))
            for endpoint in self._table.chain(task_type, mode=mode, tier_override=tier_override)
        )

    @property
    def table(self) -> RoutingTable:
        return self._table

    async def aclose(self) -> None:
        for provider in self._providers.values():
            await provider.aclose()
        self._providers.clear()


class ProviderFactory(Protocol):
    def __call__(
        self, endpoint: EndpointSpec, *, embedding_model: str, trust_env: bool
    ) -> ModelProvider: ...


def _default_provider_factory(
    endpoint: EndpointSpec, *, embedding_model: str, trust_env: bool
) -> ModelProvider:
    if endpoint.provider != "openai_compatible":
        raise ValueError(
            f"档位 {endpoint.tier} 声明的 provider {endpoint.provider!r} 尚未实现；"
            "当前只支持 openai_compatible。"
        )
    return OpenAICompatibleProvider(
        base_url=endpoint.base_url,
        api_key=endpoint.api_key,
        chat_model=endpoint.model,
        embedding_model=embedding_model,
        enable_thinking=endpoint.enable_thinking,
        timeout_s=endpoint.timeout_s,
        trust_env=trust_env,
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
        eval_run_id: UUID | None = None,
        pool: TierProviderPool | None = None,
        mode: RoutingMode = "online",
        completion_cache: CompletionCache | None = None,
        cache_ttl_s: int = 24 * 60 * 60,
    ) -> None:
        self._chat_provider = provider
        self._pool = pool
        self._mode = mode
        self._cache = completion_cache
        self._cache_ttl_s = cache_ttl_s
        self._embedding_provider = embedding_provider or provider
        # 流式接口只吐文本, 拿不到响应里的 model 字段; 但"这条答案是谁生成的"必须能记录
        # (评测要报 actual_models), 所以在网关层暴露配置身份。
        self.chat_model = self._chat_provider.chat_model
        self.chat_provider = self._chat_provider.name
        self.embedding_dimensions = embedding_dimensions
        self.embedding_model = self._embedding_provider.embedding_model
        self.embedding_provider = self._embedding_provider.name
        self.embedding_revision = embedding_revision
        self._audit_sink = audit_sink
        self._budget_guard = budget_guard
        self._pricing = pricing or GatewayPricing()
        self._chars_per_token = chars_per_token
        self._run_id = run_id
        self._eval_run_id = eval_run_id

    async def complete(
        self,
        messages: list[Message],
        *,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tier_override: Tier | None = None,
    ) -> CompletionResult:
        attempts = self._chain(task_type, tier_override=tier_override)
        cacheable = self._cache is not None and is_cacheable(
            temperature=temperature, mode=self._mode
        )
        for index, (tier, provider) in enumerate(attempts):
            is_last = index == len(attempts) - 1
            pricing = self._pricing.for_tier(tier)

            cache_key: str | None = None
            if cacheable:
                assert self._cache is not None
                cache_key = completion_cache_key(
                    tier=tier,
                    model=provider.chat_model,
                    provider=provider.name,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    # 用 getattr 是为了不逼所有测试假 provider 都实现它；
                    # 缺这个属性的假 provider 本来也不会改变请求参数。
                    request_fingerprint=getattr(provider, "request_fingerprint", ""),
                )
                started = monotonic()
                hit = await self._cache.get(cache_key)
                if hit is not None:
                    # 命中不预留也不结算: 这次调用没有发生, 成本就是 0。
                    # 但审计要记, 否则看板算不出命中率(§9)。
                    await self._audit(
                        task_type=task_type,
                        tier=tier,
                        model=hit.model,
                        provider=provider,
                        usage=hit.usage,
                        started=started,
                        success=True,
                        cost_usd=Decimal(0),
                        cache_hit=True,
                        was_fallback=index > 0,
                    )
                    return hit

            estimated_usage = Usage(
                input_tokens=self._estimate_tokens(sum(len(item.content) for item in messages)),
                output_tokens=max_tokens,
            )
            # 预留在 try 之外: 预算不足要立刻抛出去, 换个档位重试只会更快烧完额度。
            reservation = await self._reserve(pricing, estimated_usage)
            started = monotonic()
            try:
                result = await provider.complete(
                    messages, max_tokens=max_tokens, temperature=temperature
                )
            except ProviderNotDispatchedError:
                await reservation.release()
                await self._audit(
                    task_type=task_type,
                    tier=tier,
                    model=provider.chat_model,
                    provider=provider,
                    usage=Usage(),
                    started=started,
                    success=False,
                    cost_usd=Decimal(0),
                    was_fallback=index > 0,
                )
                if is_last:
                    raise
                self._log_fallback(task_type, tier, attempts[index + 1][0], dispatched=False)
                continue
            except Exception:
                # 发出去之后失败可能已经计费, 保守记账。换档位重试是第二笔钱,
                # 两笔都要留在 llm_calls 里, 否则成本曲线会把 fallback 抹平。
                await reservation.abandon()
                await self._audit(
                    task_type=task_type,
                    tier=tier,
                    model=provider.chat_model,
                    provider=provider,
                    usage=Usage(),
                    started=started,
                    success=False,
                    cost_usd=reservation.estimated_usd,
                    was_fallback=index > 0,
                )
                if is_last:
                    raise
                self._log_fallback(task_type, tier, attempts[index + 1][0], dispatched=True)
                continue
            charged = await self._settle(reservation, pricing, result.usage)
            await self._audit(
                task_type=task_type,
                tier=tier,
                model=result.model,
                provider=provider,
                usage=result.usage,
                started=started,
                success=True,
                cost_usd=charged,
                was_fallback=index > 0,
            )
            if cache_key is not None and self._cache is not None:
                # 只写成功结果: 一次抖动被钉住 24 小时比不缓存糟得多。
                await self._cache.set(cache_key, result, ttl_s=self._cache_ttl_s)
            return result
        raise AssertionError("路由链为空, chain() 应当已经抛出 TierUnavailableError")

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
        attempts = self._chain(task_type)
        for index, (tier, provider) in enumerate(attempts):
            is_last = index == len(attempts) - 1
            pricing = self._pricing.for_tier(tier)
            estimated_usage = Usage(
                input_tokens=self._estimate_tokens(sum(len(item.content) for item in messages)),
                output_tokens=max_tokens,
            )
            reservation = await self._reserve(pricing, estimated_usage)
            started = monotonic()
            produced_chars = 0
            emitted = False
            success = False
            dispatched = True
            failure: Exception | None = None
            try:
                async for chunk in provider.stream(
                    messages, max_tokens=max_tokens, temperature=temperature
                ):
                    emitted = True
                    produced_chars += len(chunk)
                    yield chunk
                success = True
            except ProviderNotDispatchedError as error:
                dispatched = False
                failure = error
            except Exception as error:
                failure = error
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
                    cost_usd = await reservation.settle(pricing.cost_usd(usage))
                else:
                    await reservation.abandon()
                    cost_usd = reservation.estimated_usd
                await self._audit(
                    task_type=task_type,
                    tier=tier,
                    model=provider.chat_model,
                    provider=provider,
                    usage=usage if success else Usage(),
                    started=started,
                    success=success,
                    cost_usd=cost_usd,
                    was_fallback=index > 0,
                )
            if success:
                return
            assert failure is not None
            # 已经吐出去的文本收不回来。此时换档位会让同一条回答的前半段和后半段
            # 由两个模型写成, 读起来是自相矛盾的一段话——比直接失败更糟。
            if emitted or is_last:
                raise failure
            self._log_fallback(task_type, tier, attempts[index + 1][0], dispatched=dispatched)

    def _chain(
        self, task_type: str, *, tier_override: Tier | None = None
    ) -> tuple[tuple[Tier, ModelProvider], ...]:
        """按 task_type 解析出可尝试的 (档位, provider) 序列。

        没有路由表时退化成单档，行为与分档之前完全一致——测试和 CLI 直接塞
        fake provider 的用法不该被路由表绑架。此时 `tier_override` 只能被忽略，
        因为根本没有第二个 provider 可选。

        `tier_override` 用于升档（docs/07 §3）：绕开 routes 直接指定档位，
        但该档位自己声明的 fallback 链仍然有效——升档之后照样可能遇到集群故障。
        """

        if self._pool is None:
            return (("main", self._chat_provider),)
        return self._pool.chain(task_type, mode=self._mode, tier_override=tier_override)

    def escalation_plan(self, task_type: str) -> tuple[Tier | None, Tier | None]:
        """返回 (起始档, 升档目标)；没有路由表时是 (None, None)，即不升档。

        调用方拿它喂给 `run_with_escalation`，这样"要不要升档、升到哪一档"
        始终由 config/routing.yaml 说了算，业务代码里不出现档位常量。
        """

        if self._pool is None:
            return (None, None)
        table = self._pool.table
        return (table.tier_for(task_type), table.escalation_for(task_type))

    def _log_fallback(self, task_type: str, source: Tier, target: Tier, *, dispatched: bool) -> None:
        logger.warning(
            "档位 fallback",
            task_type=task_type,
            from_tier=source,
            to_tier=target,
            # 已发出的调用可能已经计费, 这一笔仍然记在 source 档位上。
            source_may_be_charged=dispatched,
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
        tier: Tier = "main",
        cache_hit: bool = False,
        was_fallback: bool = False,
    ) -> None:
        if self._audit_sink is None:
            return
        trace_id = str(structlog.contextvars.get_contextvars().get("trace_id") or "local")
        await self._audit_sink.record(
            AuditRecord(
                trace_id=trace_id,
                task_type=task_type,
                # 记实际作答的档位而不是路由表上写的档位: fallback 之后两者会不一致,
                # 而成本与质量分析全靠这一列分组(docs/07 §9)。
                tier=tier,
                model=model,
                provider=provider.name,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                latency_ms=max(0, round((monotonic() - started) * 1000)),
                success=success,
                cost_usd=cost_usd,
                cached=cache_hit,
                cache_type="exact" if cache_hit else None,
                was_fallback=was_fallback,
                run_id=self._run_id,
                eval_run_id=self._eval_run_id,
            )
        )

    async def aclose(self) -> None:
        await self._chat_provider.aclose()
        if self._embedding_provider is not self._chat_provider:
            await self._embedding_provider.aclose()
        if self._pool is not None:
            await self._pool.aclose()


def gateway_pricing_from_settings(settings: Settings) -> GatewayPricing:
    return GatewayPricing(
        chat=ModelPricing(
            input_usd_per_mtok=settings.price_main_input_usd_per_mtok,
            output_usd_per_mtok=settings.price_main_output_usd_per_mtok,
        ),
        embedding=ModelPricing(
            input_usd_per_mtok=settings.price_embedding_input_usd_per_mtok,
        ),
        by_tier={
            "light": ModelPricing(
                input_usd_per_mtok=settings.price_light_input_usd_per_mtok,
                output_usd_per_mtok=settings.price_light_output_usd_per_mtok,
            ),
            "main": ModelPricing(
                input_usd_per_mtok=settings.price_main_input_usd_per_mtok,
                output_usd_per_mtok=settings.price_main_output_usd_per_mtok,
            ),
            "heavy": ModelPricing(
                input_usd_per_mtok=settings.price_heavy_input_usd_per_mtok,
                output_usd_per_mtok=settings.price_heavy_output_usd_per_mtok,
            ),
            "external": ModelPricing(
                input_usd_per_mtok=settings.price_external_input_usd_per_mtok,
                output_usd_per_mtok=settings.price_external_output_usd_per_mtok,
            ),
        },
    )


def load_settings_routing_table(settings: Settings) -> RoutingTable | None:
    """没有 routing.yaml 就退回单档。

    路由表是可选的而不是必需的：M0/M1 的部署、CLI 与测试都只有一个 endpoint，
    强制要求这个文件存在只会让它们平白多一个前置条件。
    """

    path = settings.routing_config_path
    if not path.exists():
        return None
    table = load_routing_table(path, routing_env(settings))
    drifted = table.unavailable_routes()
    if drifted:
        # 启动时说一次。静默降档不算错, 不说才是——尤其是 light 档没部署时,
        # 所有"省钱"的路由其实都在跑 main。
        logger.warning(
            "部分档位未配置 endpoint, 线上按 fallback 链降级",
            routes={task: f"{want}→{got}" for task, (want, got) in sorted(drifted.items())},
        )
    return table


def build_model_gateway(
    settings: Settings,
    *,
    audit_sink: AuditSink | None = None,
    budget_guard: BudgetGuard | None = None,
    run_id: UUID | None = None,
    eval_run_id: UUID | None = None,
    mode: RoutingMode = "online",
    completion_cache: CompletionCache | None = None,
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
    if completion_cache is None and settings.llm_cache_enabled and mode != "evaluation":
        # 延迟到这里 import: eval 与 CLI 不该因为建个网关就被拖上 Redis 依赖。
        from app.core.redis import redis_client
        from app.llm.cache import RedisCompletionCache

        completion_cache = RedisCompletionCache(redis_client)
    table = load_settings_routing_table(settings)
    pool = (
        None
        if table is None
        else TierProviderPool(
            table,
            embedding_model=settings.embedding_model,
            trust_env=settings.model_trust_env,
        )
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
        eval_run_id=eval_run_id,
        pool=pool,
        mode=mode,
        completion_cache=completion_cache,
        cache_ttl_s=settings.llm_cache_ttl_s,
    )
