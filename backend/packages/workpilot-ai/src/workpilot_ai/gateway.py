from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from time import monotonic
from typing import Protocol, cast
from uuid import UUID

import structlog
from uuid6 import uuid7

from workpilot_ai.batch import current_batch_id
from workpilot_ai.cache import CompletionCache, completion_cache_key, is_cacheable
from workpilot_ai.errors import (
    ModelContextOverflowError as ModelContextOverflowError,
)
from workpilot_ai.errors import (
    ProviderNotDispatchedError,
    ProviderResponseError,
    ProviderRouteTimeoutError,
    ProviderTimeoutError,
)
from workpilot_ai.pricing import GatewayPricing, ModelPricing, estimate_tokens, is_measured
from workpilot_ai.prompt_cache import prompt_cache_key
from workpilot_ai.providers.openai_compatible import OpenAICompatibleProvider
from workpilot_ai.routing import (
    EndpointSpec,
    RoutingMode,
    RoutingTable,
    Tier,
)
from workpilot_ai.types import (
    AuditRecord,
    AuditSink,
    BudgetGuard,
    CompletionChunk,
    CompletionResult,
    EmbeddingResult,
    Message,
    ModelProvider,
    PromptCachingToolCallingProvider,
    StreamingToolCallingProvider,
    ToolCallingProvider,
    ToolDefinition,
    Usage,
)

logger = structlog.get_logger(__name__)

_CHAT_REQUEST_OVERHEAD_TOKENS = 4
_CHAT_MESSAGE_OVERHEAD_TOKENS = 8
# Cowork 的主循环可能把整轮输出额度都花在 reasoning 上。这里的 max_tokens 仍用于
# 上下文压缩、run 计量和费用预留，但支持省略该字段的 Provider 不再收到客户端硬上限。
# 其他短任务（标题、摘要、改写等）继续严格限长。
_PROVIDER_DEFAULT_OUTPUT_TASKS = frozenset({"cowork_decision"})


class EmbeddingDimensionError(ValueError):
    pass


class EmbeddingIdentityError(ValueError):
    pass


class NativeToolCallingUnsupportedError(ProviderNotDispatchedError):
    """路由链里的 provider 都没有实现原生 tool-calling。"""


def _wire_max_tokens(
    task_type: str, provider: ModelProvider, configured_max_tokens: int
) -> int | None:
    if task_type in _PROVIDER_DEFAULT_OUTPUT_TASKS and bool(
        getattr(provider, "supports_omitting_max_tokens", False)
    ):
        return None
    return configured_max_tokens


def request_character_count(
    messages: list[Message], tools: list[ToolDefinition] | None = None
) -> int:
    """估算 canonical 消息与工具 schema 的请求体字符量。"""

    total = 0
    for message in messages:
        total += len(message.content)
        for attachment in message.attachments:
            # 图片由 provider 作为视觉 token 计费；PDF/文本的提取正文按实际字符
            # 进入保守预算。固定开销覆盖图片编码与文档容器。
            total += len(attachment.extracted_text) + 2_048
        if message.tool_call_id is not None:
            total += len(message.tool_call_id)
        total += sum(
            len(call.id) + len(call.name) + len(call.arguments) for call in message.tool_calls
        )
    if tools is not None:
        total += sum(
            len(tool.name) + len(tool.description) + len(str(tool.parameters)) for tool in tools
        )
    return total


@dataclass(frozen=True)
class PromptBudget:
    """一次具体模型调用的上下文预算。

    当前没有在服务进程内加载各模型 tokenizer；发送前按 UTF-8 字节数估算，取
    1 byte = 1 token 的保守上界，并额外计入消息包装与安全余量。字节级 BPE 的 token
    数不会高于输入字节数，因此中文、生僻字和 emoji 都不会被字符均值低估；真正的
    provider usage 仍用于调用后审计。
    """

    task_type: str
    tier: Tier
    model: str
    context_window_tokens: int
    max_output_tokens: int
    safety_tokens: int

    @property
    def max_input_tokens(self) -> int:
        return max(0, self.context_window_tokens - self.max_output_tokens - self.safety_tokens)

    def estimate_messages_tokens(
        self, messages: list[Message], tools: list[ToolDefinition] | None = None
    ) -> int:
        content_tokens = request_character_count(messages, tools)
        return (
            content_tokens
            + _CHAT_REQUEST_OVERHEAD_TOKENS
            + _CHAT_MESSAGE_OVERHEAD_TOKENS * len(messages)
        )

    def fits(self, messages: list[Message], tools: list[ToolDefinition] | None = None) -> bool:
        return self.estimate_messages_tokens(messages, tools) <= self.max_input_tokens


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

    同一 endpoint（包括任务级 timeout）复用同一个实例/HTTP 连接池；不同档位或超时
    即使指向同一台机器也分开，因为 provider 的身份包含 (base_url, model, timeout)。
    """

    def __init__(
        self,
        table: RoutingTable,
        *,
        embedding_model: str,
        trust_env: bool,
        openai_compatible_prompt_cache_key_supported: bool = False,
        factory: "ProviderFactory | None" = None,
    ) -> None:
        self._table = table
        self._embedding_model = embedding_model
        self._trust_env = trust_env
        self._factory = factory
        self._openai_compatible_prompt_cache_key_supported = (
            openai_compatible_prompt_cache_key_supported
        )
        # 同一档位可以被不同 task_type 赋予不同 timeout；EndpointSpec 必须进入缓存键，
        # 否则先创建的 30 秒 Provider 会让后续 Cowork 的 120 秒覆盖静默失效。
        self._providers: dict[EndpointSpec, ModelProvider] = {}

    def _provider(self, endpoint: EndpointSpec) -> ModelProvider:
        cached = self._providers.get(endpoint)
        if cached is None:
            cached = (
                _default_provider_factory(
                    endpoint,
                    embedding_model=self._embedding_model,
                    trust_env=self._trust_env,
                    prompt_cache_key_supported=(self._openai_compatible_prompt_cache_key_supported),
                )
                if self._factory is None
                else self._factory(
                    endpoint,
                    embedding_model=self._embedding_model,
                    trust_env=self._trust_env,
                )
            )
            self._providers[endpoint] = cached
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
    endpoint: EndpointSpec,
    *,
    embedding_model: str,
    trust_env: bool,
    prompt_cache_key_supported: bool = False,
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
        prompt_cache_key_supported=prompt_cache_key_supported,
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
        provider_prompt_cache_enabled: bool = True,
        default_context_window_tokens: int = 32_768,
        context_safety_tokens: int = 512,
    ) -> None:
        self._chat_provider = provider
        self._pool = pool
        self._mode = mode
        self._cache = completion_cache
        self._cache_ttl_s = cache_ttl_s
        self._provider_prompt_cache_enabled = provider_prompt_cache_enabled
        self._default_context_window_tokens = default_context_window_tokens
        self._context_safety_tokens = context_safety_tokens
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
        return await self._complete(
            messages,
            task_type=task_type,
            max_tokens=max_tokens,
            temperature=temperature,
            tier_override=tier_override,
            tools=None,
            parallel_tool_calls=False,
        )

    async def complete_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool = True,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tier_override: Tier | None = None,
    ) -> CompletionResult:
        if not tools:
            raise ValueError("原生 tool-calling 至少需要一个工具")
        return await self._complete(
            messages,
            task_type=task_type,
            max_tokens=max_tokens,
            temperature=temperature,
            tier_override=tier_override,
            tools=tools,
            parallel_tool_calls=parallel_tool_calls,
        )

    async def _complete(
        self,
        messages: list[Message],
        *,
        task_type: str,
        max_tokens: int,
        temperature: float,
        tier_override: Tier | None,
        tools: list[ToolDefinition] | None,
        parallel_tool_calls: bool,
    ) -> CompletionResult:
        attempts = self._chain(task_type, tier_override=tier_override)
        cacheable = (
            tools is None
            and self._cache is not None
            and is_cacheable(temperature=temperature, mode=self._mode)
        )
        route_attempts = 0
        route_only_timed_out = True
        for index, (tier, provider) in enumerate(attempts):
            is_last = index == len(attempts) - 1
            pricing = self._pricing.for_tier(tier)
            prompt_budget = self._prompt_budget_for(
                task_type=task_type,
                tier=tier,
                provider=provider,
                max_tokens=max_tokens,
            )
            if not prompt_budget.fits(messages, tools):
                route_only_timed_out = False
                if is_last:
                    raise self._context_overflow(prompt_budget, messages, tools)
                self._log_context_fallback(
                    prompt_budget,
                    attempts[index + 1][0],
                    messages=messages,
                    tools=tools,
                )
                continue

            wire_max_tokens = _wire_max_tokens(task_type, provider, max_tokens)

            tool_method = None
            prompt_cache_method = None
            if tools is not None:
                candidate = getattr(provider, "complete_with_tools", None)
                if not callable(candidate):
                    route_only_timed_out = False
                    if is_last:
                        raise NativeToolCallingUnsupportedError(
                            f"provider {provider.name}/{provider.chat_model} 不支持原生 tool-calling"
                        )
                    self._log_fallback(task_type, tier, attempts[index + 1][0], dispatched=False)
                    continue
                tool_method = cast("ToolCallingProvider", provider).complete_with_tools
                prompt_candidate = getattr(provider, "complete_with_tools_prompt_cache", None)
                if (
                    self._provider_prompt_cache_enabled
                    and self._mode != "evaluation"
                    and callable(prompt_candidate)
                ):
                    prompt_cache_method = cast(
                        "PromptCachingToolCallingProvider", provider
                    ).complete_with_tools_prompt_cache

            cache_key: str | None = None
            if cacheable:
                assert self._cache is not None
                cache_key = completion_cache_key(
                    tier=tier,
                    model=provider.chat_model,
                    provider=provider.name,
                    messages=messages,
                    max_tokens=wire_max_tokens,
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
                input_tokens=self._estimate_tokens(request_character_count(messages, tools)),
                output_tokens=max_tokens,
            )
            # 预留在 try 之外: 预算不足要立刻抛出去, 换个档位重试只会更快烧完额度。
            reservation = await self._reserve(pricing, estimated_usage)
            started = monotonic()
            route_attempts += 1
            try:
                if tools is None:
                    result = await provider.complete(
                        messages, max_tokens=wire_max_tokens, temperature=temperature
                    )
                elif prompt_cache_method is not None:
                    result = await prompt_cache_method(
                        messages,
                        tools=tools,
                        parallel_tool_calls=parallel_tool_calls,
                        max_tokens=wire_max_tokens,
                        temperature=temperature,
                        prompt_cache_key=prompt_cache_key(
                            provider=provider.name,
                            model=provider.chat_model,
                            task_type=task_type,
                            messages=messages,
                            tools=tools,
                        ),
                    )
                else:
                    assert tool_method is not None
                    result = await tool_method(
                        messages,
                        tools=tools,
                        parallel_tool_calls=parallel_tool_calls,
                        max_tokens=wire_max_tokens,
                        temperature=temperature,
                    )
            except ProviderNotDispatchedError:
                route_only_timed_out = False
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
            except Exception as error:
                if not isinstance(error, ProviderTimeoutError):
                    route_only_timed_out = False
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
                    if route_attempts > 0 and route_only_timed_out:
                        raise ProviderRouteTimeoutError(
                            f"模型路由 {task_type} 的全部可用 endpoint 均响应超时"
                        ) from error
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

    def stream_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool = True,
        task_type: str = "generate",
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tier_override: Tier | None = None,
    ) -> AsyncIterator[CompletionChunk]:
        """`complete_with_tools` 的流式版本：一路 delta，最后一块携带完整结果。

        契约刻意做成"终块 = complete_with_tools 的返回值"，这样 Agent 循环的决策逻辑
        完全不需要为流式改写——它照旧拿到一个 `CompletionResult`，只是在等待期间多了
        可以转播给用户的增量。

        **不做结果缓存**：tool-calling 那条路本来就不缓存（同一段前缀在不同工具面下
        应当得出不同决策），流式更不该缓存，否则重放的"流"是假的。

        **provider 不支持流式就在同一个 endpoint 上降级**成一次 `complete_with_tools`，
        只发终块。降级发生在档位内部而不是往下一档掉：不支持流式不是这个 endpoint 有
        问题，换成更贵的一档既解决不了问题又悄悄改了模型。
        """

        if not tools:
            raise ValueError("原生 tool-calling 至少需要一个工具")
        return self._stream_with_tools(
            messages,
            tools=tools,
            parallel_tool_calls=parallel_tool_calls,
            task_type=task_type,
            max_tokens=max_tokens,
            temperature=temperature,
            tier_override=tier_override,
        )

    async def _stream_with_tools(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition],
        parallel_tool_calls: bool,
        task_type: str,
        max_tokens: int,
        temperature: float,
        tier_override: Tier | None,
    ) -> AsyncIterator[CompletionChunk]:
        attempts = self._chain(task_type, tier_override=tier_override)
        # 与 `_complete` 同一套判据：整条路由链只是超时（而不是任何一次真正的失败）时，
        # 抛的是 `ProviderRouteTimeoutError`——调用方据此把 run 挂起等会儿重来，而不是
        # 判死。少了这两个变量，一次全链超时会被当成普通失败，run 直接落 failed。
        route_attempts = 0
        route_only_timed_out = True
        for index, (tier, provider) in enumerate(attempts):
            is_last = index == len(attempts) - 1
            pricing = self._pricing.for_tier(tier)
            prompt_budget = self._prompt_budget_for(
                task_type=task_type,
                tier=tier,
                provider=provider,
                max_tokens=max_tokens,
            )
            if not prompt_budget.fits(messages, tools):
                route_only_timed_out = False
                if is_last:
                    raise self._context_overflow(prompt_budget, messages, tools)
                self._log_context_fallback(
                    prompt_budget, attempts[index + 1][0], messages=messages, tools=tools
                )
                continue

            wire_max_tokens = _wire_max_tokens(task_type, provider, max_tokens)

            stream_candidate = getattr(provider, "stream_with_tools", None)
            tool_candidate = getattr(provider, "complete_with_tools", None)
            if not callable(stream_candidate) and not callable(tool_candidate):
                route_only_timed_out = False
                if is_last:
                    raise NativeToolCallingUnsupportedError(
                        f"provider {provider.name}/{provider.chat_model} 不支持原生 tool-calling"
                    )
                self._log_fallback(task_type, tier, attempts[index + 1][0], dispatched=False)
                continue

            estimated_usage = Usage(
                input_tokens=self._estimate_tokens(request_character_count(messages, tools)),
                output_tokens=max_tokens,
            )
            reservation = await self._reserve(pricing, estimated_usage)
            started = monotonic()
            route_attempts += 1
            emitted = False
            result: CompletionResult | None = None
            dispatched = True
            failure: Exception | None = None
            try:
                if callable(stream_candidate):
                    provider_stream = cast(
                        "StreamingToolCallingProvider", provider
                    ).stream_with_tools(
                        messages,
                        tools=tools,
                        parallel_tool_calls=parallel_tool_calls,
                        max_tokens=wire_max_tokens,
                        temperature=temperature,
                    )
                    async for chunk in provider_stream:
                        if chunk.result is not None:
                            result = chunk.result
                            continue
                        if chunk.text_delta or chunk.reasoning_delta:
                            emitted = True
                            yield chunk
                    if result is None:
                        raise ProviderResponseError(
                            f"provider {provider.name} 的流没有给出终块，"
                            "拿不到这一轮的完整结果与用量"
                        )
                else:
                    result = await cast("ToolCallingProvider", provider).complete_with_tools(
                        messages,
                        tools=tools,
                        parallel_tool_calls=parallel_tool_calls,
                        max_tokens=wire_max_tokens,
                        temperature=temperature,
                    )
            except ProviderNotDispatchedError as error:
                route_only_timed_out = False
                dispatched = False
                failure = error
            except Exception as error:
                if not isinstance(error, ProviderTimeoutError):
                    route_only_timed_out = False
                failure = error

            if failure is None:
                assert result is not None
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
                yield CompletionChunk(result=result)
                return

            if not dispatched:
                await reservation.release()
                cost_usd = Decimal(0)
            else:
                await reservation.abandon()
                cost_usd = reservation.estimated_usd
            await self._audit(
                task_type=task_type,
                tier=tier,
                model=provider.chat_model,
                provider=provider,
                usage=Usage(),
                started=started,
                success=False,
                cost_usd=cost_usd,
                was_fallback=index > 0,
            )
            # 与 `_stream` 同一条规则：已经吐出去的文本收不回来。此时换档位会让同一段
            # 回答的前半段和后半段由两个模型写成，读起来是自相矛盾的一段话。
            if emitted or is_last:
                if route_attempts > 0 and route_only_timed_out:
                    raise ProviderRouteTimeoutError(
                        f"模型路由 {task_type} 的全部可用 endpoint 均响应超时"
                    ) from failure
                raise failure
            self._log_fallback(task_type, tier, attempts[index + 1][0], dispatched=dispatched)
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
            prompt_budget = self._prompt_budget_for(
                task_type=task_type,
                tier=tier,
                provider=provider,
                max_tokens=max_tokens,
            )
            if not prompt_budget.fits(messages):
                if is_last:
                    raise self._context_overflow(prompt_budget, messages)
                self._log_context_fallback(
                    prompt_budget,
                    attempts[index + 1][0],
                    messages=messages,
                )
                continue
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

    def prompt_budget(
        self,
        task_type: str,
        *,
        max_tokens: int,
        tier_override: Tier | None = None,
    ) -> PromptBudget:
        """返回该任务首选可用档位的预算，供业务层在调用前裁剪可选上下文。"""

        tier, provider = self._chain(task_type, tier_override=tier_override)[0]
        return self._prompt_budget_for(
            task_type=task_type,
            tier=tier,
            provider=provider,
            max_tokens=max_tokens,
        )

    def _prompt_budget_for(
        self,
        *,
        task_type: str,
        tier: Tier,
        provider: ModelProvider,
        max_tokens: int,
    ) -> PromptBudget:
        context_window_tokens = self._default_context_window_tokens
        if self._pool is not None:
            context_window_tokens = self._pool.table.tiers[tier].primary.context_window_tokens
        return PromptBudget(
            task_type=task_type,
            tier=tier,
            model=provider.chat_model,
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_tokens,
            safety_tokens=self._context_safety_tokens,
        )

    @staticmethod
    def _context_overflow(
        budget: PromptBudget,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
    ) -> ModelContextOverflowError:
        estimated = budget.estimate_messages_tokens(messages, tools)
        return ModelContextOverflowError(
            f"任务 {budget.task_type} 的输入约 {estimated} tokens，输出预留 "
            f"{budget.max_output_tokens} tokens，超过 {budget.tier}/{budget.model} 的 "
            f"{budget.context_window_tokens}-token 上下文窗口"
        )

    @staticmethod
    def _log_context_fallback(
        budget: PromptBudget,
        target: Tier,
        *,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
    ) -> None:
        logger.warning(
            "模型上下文窗口不足，发送前切换档位",
            task_type=budget.task_type,
            from_tier=budget.tier,
            to_tier=target,
            estimated_input_tokens=budget.estimate_messages_tokens(messages, tools),
            reserved_output_tokens=budget.max_output_tokens,
            context_window_tokens=budget.context_window_tokens,
        )

    def _log_fallback(
        self, task_type: str, source: Tier, target: Tier, *, dispatched: bool
    ) -> None:
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
                prompt_cache_read_tokens=(0 if cache_hit else usage.prompt_cache_read_tokens),
                prompt_cache_write_tokens=(0 if cache_hit else usage.prompt_cache_write_tokens),
                latency_ms=max(0, round((monotonic() - started) * 1000)),
                success=success,
                cost_usd=cost_usd,
                cached=cache_hit,
                cache_type="exact" if cache_hit else None,
                was_fallback=was_fallback,
                # 只有显式开了批次才有值; 线上单条问答保持 NULL(docs/07 §7.2)。
                batch_id=current_batch_id(),
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
