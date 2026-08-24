"""Settings → ModelGateway 的组装根（composition root）。

`workpilot_ai` 是「只懂模型」的一层，它不认识 `Settings`、`.env` 与 Redis 连接串；
把部署配置翻译成网关构造参数是**应用层**的职责，因此这些 `*_from_settings` 适配器
留在这里而不是包里（docs/adr/0011）。

约束 1（所有 LLM 调用必须经过模型网关）不变：本模块是构造入口，
业务代码拿到的仍然只有 `workpilot_ai.gateway.ModelGateway`。
"""

from uuid import UUID

import structlog

from app.core.config import Settings
from workpilot_ai.cache import CompletionCache, shared_completion_cache
from workpilot_ai.gateway import ModelGateway, TierProviderPool
from workpilot_ai.pricing import GatewayPricing, ModelPricing
from workpilot_ai.providers.openai_compatible import OpenAICompatibleProvider
from workpilot_ai.routing import RoutingMode, RoutingTable, load_routing_table
from workpilot_ai.types import AuditSink, BudgetGuard, ModelProvider

logger = structlog.get_logger(__name__)


def routing_env(settings: Settings) -> dict[str, str]:
    """路由表里允许出现的 `${NAME}` 全集。

    刻意不回落到 `os.environ`：一是 pydantic-settings 读 `.env` 时并不写回环境变量，
    二是白名单能让"配置里写错一个变量名"在加载期就报错，而不是拿到空串后
    在半夜表现为"某个档位神秘地不可用"。
    """

    def flag(value: bool | None) -> str:
        return "" if value is None else str(value).lower()

    return {
        "TIER_LIGHT_BASE_URL": settings.tier_light_base_url,
        "TIER_LIGHT_MODEL": settings.tier_light_model,
        "TIER_LIGHT_ENABLE_THINKING": flag(settings.tier_light_enable_thinking),
        "TIER_LIGHT_CONTEXT_WINDOW_TOKENS": str(settings.tier_light_context_window_tokens),
        "TIER_MAIN_BASE_URL": settings.tier_main_base_url,
        "TIER_MAIN_MODEL": settings.tier_main_model,
        "TIER_MAIN_ENABLE_THINKING": flag(settings.tier_main_enable_thinking),
        "TIER_MAIN_CONTEXT_WINDOW_TOKENS": str(settings.tier_main_context_window_tokens),
        "TIER_HEAVY_BASE_URL": settings.tier_heavy_base_url,
        "TIER_HEAVY_MODEL": settings.tier_heavy_model,
        "TIER_HEAVY_ENABLE_THINKING": flag(settings.tier_heavy_enable_thinking),
        "TIER_HEAVY_CONTEXT_WINDOW_TOKENS": str(settings.tier_heavy_context_window_tokens),
        "TIER_EXTERNAL_BASE_URL": settings.tier_external_base_url,
        "TIER_EXTERNAL_MODEL": settings.tier_external_model,
        "TIER_EXTERNAL_CONTEXT_WINDOW_TOKENS": str(settings.tier_external_context_window_tokens),
        "COWORK_MODEL_TIMEOUT_S": str(settings.cowork_model_timeout_s),
        "EXTERNAL_API_KEY": settings.external_api_key,
        "CLUSTER_API_KEY": settings.cluster_api_key,
    }


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
        prompt_cache_key_supported=settings.openai_compatible_prompt_cache_key_enabled,
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
        # 进程级单例：每建一个网关就新建一个缓存等于恒不命中。
        completion_cache = shared_completion_cache(max_entries=settings.llm_cache_max_entries)
    table = load_settings_routing_table(settings)
    pool = (
        None
        if table is None
        else TierProviderPool(
            table,
            embedding_model=settings.embedding_model,
            trust_env=settings.model_trust_env,
            openai_compatible_prompt_cache_key_supported=(
                settings.openai_compatible_prompt_cache_key_enabled
            ),
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
        provider_prompt_cache_enabled=settings.provider_prompt_cache_enabled,
        default_context_window_tokens=settings.tier_main_context_window_tokens,
        context_safety_tokens=settings.llm_context_safety_tokens,
    )


def build_custom_model_gateway(
    settings: Settings,
    *,
    chat_provider: ModelProvider,
    context_window_tokens: int,
    audit_sink: AuditSink | None = None,
    budget_guard: BudgetGuard | None = None,
    run_id: UUID | None = None,
    completion_cache: CompletionCache | None = None,
) -> ModelGateway:
    """为会话显式选择的 Provider 构造网关。

    只替换 chat provider；资料库 embedding 身份继续使用部署级配置，避免用户切换
    对话模型后把同一资料库写进另一个向量空间。
    """

    embedding_provider = OpenAICompatibleProvider(
        base_url=settings.embedding_base_url or settings.tier_main_base_url,
        api_key=settings.cluster_api_key,
        chat_model=settings.tier_main_model,
        embedding_model=settings.embedding_model,
        timeout_s=settings.model_timeout_s,
        trust_env=settings.model_trust_env,
    )
    if completion_cache is None and settings.llm_cache_enabled:
        completion_cache = shared_completion_cache(max_entries=settings.llm_cache_max_entries)
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
        completion_cache=completion_cache,
        cache_ttl_s=settings.llm_cache_ttl_s,
        provider_prompt_cache_enabled=settings.provider_prompt_cache_enabled,
        default_context_window_tokens=context_window_tokens,
        context_safety_tokens=settings.llm_context_safety_tokens,
    )
