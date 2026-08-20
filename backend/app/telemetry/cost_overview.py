"""成本看板的读模型（docs/07 §7.3）。

只读、聚合、给人看。与 `cost_report.py` 的分工：那个是按批摊销的**计算口径**，
被评测跑批与 CLI 复用；这里是把它和 `llm_calls` 的调用画像拼成一个页面。

两条写死的口径，改之前先读 docs/07 §7.3–7.4：

1. **缺价格给 `null`，不给 `0.00`。** 本机自部署价格表是 0，
   "没有可用价格"和"测过、就是不要钱"是两回事，混在一起看板就开始骗人。
2. **成本必须和吞吐、并发度、占用率一起出现。** 同一个模型在并发 1 与并发 16 下
   单位成本能差一个数量级，单报一个"每次问答多少钱"没法解释也没法比较。
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm_bootstrap import load_settings_routing_table
from app.schemas.cost import (
    BatchSummary,
    CostOverviewResponse,
    CostTotals,
    TaskTypeUsage,
    TierUsage,
)
from app.telemetry.cost_report import BatchCost, load_batch_costs
from workpilot_ai.routing import TIERS

_COST_UNAVAILABLE = "unavailable"
_COST_OK = "ok"
_NO_PRICE_REASON = "当前模型价格表为 0(自部署), 没有可报告的金额; token 用量才是成本口径"

# 缓存命中不消耗 GPU，也不参与摊销分母——算进去等于把缓存收益重复计一遍。
# 但命中**次数**要单列，它是缓存收益的唯一直接证据。
_TIER_SQL = """
SELECT tier,
       count(*)                                         AS call_count,
       count(*) FILTER (WHERE cached)                   AS cached_count,
       count(*) FILTER (WHERE NOT success)              AS failed_count,
       count(*) FILTER (WHERE was_fallback)             AS fallback_count,
       COALESCE(sum(prompt_cache_read_tokens) FILTER (WHERE NOT cached), 0)
                                                           AS prompt_cache_read_tokens,
       COALESCE(sum(prompt_cache_write_tokens) FILTER (WHERE NOT cached), 0)
                                                           AS prompt_cache_write_tokens,
       COALESCE(sum(prompt_tokens) FILTER (WHERE NOT cached), 0) AS prompt_tokens,
       COALESCE(sum(output_tokens) FILTER (WHERE NOT cached), 0) AS output_tokens,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY latency_ms)
           FILTER (WHERE NOT cached)                    AS p50_latency_ms,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms)
           FILTER (WHERE NOT cached)                    AS p95_latency_ms,
       array_agg(DISTINCT model ORDER BY model)         AS models
FROM llm_calls
WHERE created_at >= :window_from
GROUP BY tier
ORDER BY tier
"""

_TASK_TYPE_SQL = """
SELECT task_type, tier,
       count(*)                                         AS call_count,
       count(*) FILTER (WHERE cached)                   AS cached_count,
       COALESCE(sum(prompt_cache_read_tokens) FILTER (WHERE NOT cached), 0)
                                                           AS prompt_cache_read_tokens,
       COALESCE(sum(prompt_cache_write_tokens) FILTER (WHERE NOT cached), 0)
                                                           AS prompt_cache_write_tokens,
       COALESCE(sum(prompt_tokens + output_tokens) FILTER (WHERE NOT cached), 0) AS total_tokens
FROM llm_calls
WHERE created_at >= :window_from
GROUP BY task_type, tier
ORDER BY count(*) DESC, task_type, tier
"""

_WINDOW_SQL = """
SELECT min(created_at) AS window_from, max(created_at) AS window_to
FROM llm_calls
WHERE created_at >= :window_from
"""


async def get_cost_overview(
    session: AsyncSession, *, settings: Settings, days: int = 30, label: str | None = None
) -> CostOverviewResponse:
    window_from = datetime.now(UTC) - timedelta(days=days)
    tiers = await _load_tiers(session, window_from)
    task_types = await _load_task_types(session, window_from)
    batches = [
        summary
        for summary in (
            _batch_summary(batch) for batch in await load_batch_costs(session, label=label)
        )
        if summary is not None
    ]
    window = (
        (await session.execute(text(_WINDOW_SQL), {"window_from": window_from})).mappings().one()
    )
    return CostOverviewResponse(
        totals=_totals(
            tiers,
            batches,
            window_from=_optional_datetime(window["window_from"]),
            window_to=_optional_datetime(window["window_to"]),
        ),
        by_tier=tiers,
        by_task_type=task_types,
        batches=batches,
        undeployed_tiers=_undeployed_tiers(settings),
    )


async def _load_tiers(session: AsyncSession, window_from: datetime) -> list[TierUsage]:
    rows = (await session.execute(text(_TIER_SQL), {"window_from": window_from})).mappings().all()
    return [
        TierUsage(
            tier=str(row["tier"]),
            call_count=int(row["call_count"]),
            cached_count=int(row["cached_count"]),
            failed_count=int(row["failed_count"]),
            fallback_count=int(row["fallback_count"]),
            prompt_cache_read_tokens=int(row["prompt_cache_read_tokens"]),
            prompt_cache_write_tokens=int(row["prompt_cache_write_tokens"]),
            prompt_cache_read_rate=_rate(
                int(row["prompt_cache_read_tokens"]), int(row["prompt_tokens"])
            ),
            prompt_tokens=int(row["prompt_tokens"]),
            output_tokens=int(row["output_tokens"]),
            total_tokens=int(row["prompt_tokens"]) + int(row["output_tokens"]),
            cache_hit_rate=_rate(int(row["cached_count"]), int(row["call_count"])),
            p50_latency_ms=_optional_int(row["p50_latency_ms"]),
            p95_latency_ms=_optional_int(row["p95_latency_ms"]),
            models=[str(model) for model in (row["models"] or [])],
        )
        for row in rows
    ]


async def _load_task_types(session: AsyncSession, window_from: datetime) -> list[TaskTypeUsage]:
    rows = (
        (await session.execute(text(_TASK_TYPE_SQL), {"window_from": window_from})).mappings().all()
    )
    return [
        TaskTypeUsage(
            task_type=str(row["task_type"]),
            tier=str(row["tier"]),
            call_count=int(row["call_count"]),
            total_tokens=int(row["total_tokens"]),
            cache_hit_rate=_rate(int(row["cached_count"]), int(row["call_count"])),
            prompt_cache_read_tokens=int(row["prompt_cache_read_tokens"]),
            prompt_cache_write_tokens=int(row["prompt_cache_write_tokens"]),
        )
        for row in rows
    ]


def _batch_summary(batch: BatchCost) -> BatchSummary | None:
    # 没有任何计费调用的批次不上看板：分母是 0 时每一项单位成本都没有意义
    if batch.task_count == 0:
        return None
    priced = batch.gpu_cost_usd is not None
    return BatchSummary(
        batch_id=str(batch.batch_id),
        label=batch.label,
        tier=batch.tier,
        model=batch.model,
        gpu_model=batch.gpu_model,
        node_count=batch.node_count,
        task_count=batch.task_count,
        total_tokens=batch.total_tokens,
        output_tokens=batch.output_tokens,
        wall_s=str(batch.wall_s),
        gpu_s=str(batch.gpu_s),
        gpu_s_per_task=str(batch.gpu_s_per_task),
        tokens_per_task=batch.tokens_per_task,
        tasks_per_s=str(batch.tasks_per_s),
        tokens_per_s=str(batch.tokens_per_s),
        mean_concurrency=str(batch.mean_concurrency),
        client_occupancy=str(batch.client_occupancy),
        price_usd_per_hour=_optional_str(batch.price_usd_per_hour),
        price_source=batch.price_source,
        cost_usd=_optional_str(batch.gpu_cost_usd),
        cost_per_task_usd=_optional_str(batch.cost_per_task_usd),
        cost_per_ktok_usd=_optional_str(batch.cost_per_ktok_usd),
        cost_status=_COST_OK if priced else _COST_UNAVAILABLE,
        cost_reason=None if priced else _NO_PRICE_REASON,
    )


def _totals(
    tiers: list[TierUsage],
    batches: list[BatchSummary],
    *,
    window_from: datetime | None,
    window_to: datetime | None,
) -> CostTotals:
    call_count = sum(tier.call_count for tier in tiers)
    cached_count = sum(tier.cached_count for tier in tiers)
    priced = [batch for batch in batches if batch.cost_usd is not None]
    # 只要有一个批次缺价格，总额就是不完整的——标 partial 而不是把缺的按 0 加进去
    cost_status = (
        _COST_UNAVAILABLE
        if not priced
        else (_COST_OK if len(priced) == len(batches) else "partial")
    )
    return CostTotals(
        call_count=call_count,
        cached_count=cached_count,
        cache_hit_rate=_rate(cached_count, call_count),
        prompt_cache_read_tokens=sum(
            tier.prompt_cache_read_tokens for tier in tiers
        ),
        prompt_cache_write_tokens=sum(
            tier.prompt_cache_write_tokens for tier in tiers
        ),
        prompt_cache_read_rate=_rate(
            sum(tier.prompt_cache_read_tokens for tier in tiers),
            sum(tier.prompt_tokens for tier in tiers),
        ),
        total_tokens=sum(tier.total_tokens for tier in tiers),
        failed_count=sum(tier.failed_count for tier in tiers),
        fallback_count=sum(tier.fallback_count for tier in tiers),
        batch_count=len(batches),
        priced_batch_count=len(priced),
        unpriced_batch_count=len(batches) - len(priced),
        cost_usd=(
            None
            if not priced
            else str(sum((Decimal(batch.cost_usd or "0") for batch in priced), Decimal(0)))
        ),
        cost_status=cost_status,
        window_from=window_from,
        window_to=window_to,
    )


def _undeployed_tiers(settings: Settings) -> list[str]:
    """路由表里声明了但没有 endpoint 的档位。

    看板必须说清楚这件事：light 没部署时，所有"省钱"的路由其实都在跑 main，
    而档位分布图看上去只是"light 有 0 次调用"——那读起来像"没人用"，
    不像"用不了"。两者的结论完全相反。
    """

    table = load_settings_routing_table(settings)
    if table is None:
        return []
    return [
        tier for tier in TIERS if tier in table.tiers and not table.tiers[tier].primary.available
    ]


def _rate(part: int, whole: int) -> float:
    return 0.0 if whole == 0 else round(part / whole, 4)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)  # type: ignore[call-overload]


def _optional_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _optional_datetime(value: object) -> datetime | None:
    return value if isinstance(value, datetime) else None
