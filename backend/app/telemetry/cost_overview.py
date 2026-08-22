"""成本看板的读模型（docs/07 §7.3）。

只读、聚合、给人看。

两条写死的口径，改之前先读 docs/07 §7.3–7.4：

1. **缺价格给 `null`，不给 `0.00`。** 本机自部署价格表是 0，
   "没有可用价格"和"测过、就是不要钱"是两回事，混在一起看板就开始骗人。所以这里
   单独统计 `unpriced_count`：一旦有调用没有单价，总额就标成 `partial`。
2. **成本必须和 token 用量一起出现。** 单报一个"每次问答多少钱"没法解释也没法比较。

**批次摊销那一段删掉了。** 它按 `gpu_batches` 算整批 GPU 墙钟摊销，只有评测跑批会往那张
表里写；日常使用一条都不产生。看板上永远空着的一块比没有更糟——它让人以为成本口径失灵了。
口径本身仍留在 `packages/workpilot-telemetry/cost.py`，评测重启时接回来即可。

聚合跑在 SQLite 上，所以没有 `FILTER`、`percentile_disc`、`array_agg`：
分组用 `SUM(CASE WHEN ...)`，分位数在 Python 里算。个人使用的调用量下，把一个窗口的
latency 拉回内存排序是毫秒级的事，为此写一段 SQL 分位数近似不值得。
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.config import Settings
from app.llm_bootstrap import load_settings_routing_table
from app.schemas.cost import (
    CostOverviewResponse,
    CostTotals,
    TaskTypeUsage,
    TierUsage,
)
from app.telemetry.sqlite import MICRO, SqliteTelemetryStore
from workpilot_ai.routing import TIERS

_COST_UNAVAILABLE = "unavailable"
_COST_OK = "ok"
_COST_PARTIAL = "partial"

_TIER_SQL = """
SELECT tier,
       COUNT(*) AS call_count,
       SUM(CASE WHEN cached THEN 1 ELSE 0 END) AS cached_count,
       SUM(CASE WHEN success THEN 0 ELSE 1 END) AS failed_count,
       SUM(CASE WHEN was_fallback THEN 1 ELSE 0 END) AS fallback_count,
       SUM(CASE WHEN cached THEN 0 ELSE prompt_cache_read_tokens END)
           AS prompt_cache_read_tokens,
       SUM(CASE WHEN cached THEN 0 ELSE prompt_cache_write_tokens END)
           AS prompt_cache_write_tokens,
       SUM(CASE WHEN cached THEN 0 ELSE prompt_tokens END) AS prompt_tokens,
       SUM(CASE WHEN cached THEN 0 ELSE output_tokens END) AS output_tokens,
       GROUP_CONCAT(DISTINCT model) AS models
FROM llm_calls
WHERE created_at >= :window_from
GROUP BY tier
ORDER BY tier
"""

_TASK_TYPE_SQL = """
SELECT task_type, tier,
       COUNT(*) AS call_count,
       SUM(CASE WHEN cached THEN 1 ELSE 0 END) AS cached_count,
       SUM(CASE WHEN cached THEN 0 ELSE prompt_cache_read_tokens END)
           AS prompt_cache_read_tokens,
       SUM(CASE WHEN cached THEN 0 ELSE prompt_cache_write_tokens END)
           AS prompt_cache_write_tokens,
       SUM(CASE WHEN cached THEN 0 ELSE prompt_tokens + output_tokens END) AS total_tokens
FROM llm_calls
WHERE created_at >= :window_from
GROUP BY task_type, tier
ORDER BY COUNT(*) DESC, task_type, tier
"""

_COST_SQL = """
SELECT SUM(COALESCE(cost_micro_usd, 0)) AS cost_micro_usd,
       SUM(CASE WHEN cost_micro_usd IS NULL THEN 1 ELSE 0 END) AS unpriced_count,
       SUM(CASE WHEN cost_micro_usd IS NULL THEN 0 ELSE 1 END) AS priced_count,
       MIN(created_at) AS window_from,
       MAX(created_at) AS window_to
FROM llm_calls
WHERE created_at >= :window_from
"""

_LATENCY_SQL = """
SELECT tier, latency_ms FROM llm_calls
WHERE created_at >= :window_from AND NOT cached
"""


async def get_cost_overview(
    store: SqliteTelemetryStore, *, settings: Settings, days: int = 30
) -> CostOverviewResponse:
    window_from = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    params = (window_from,)

    tier_rows = await store._read(
        lambda c: c.execute(_TIER_SQL.replace(":window_from", "?"), params).fetchall()
    )
    task_rows = await store._read(
        lambda c: c.execute(_TASK_TYPE_SQL.replace(":window_from", "?"), params).fetchall()
    )
    cost_row = await store._read(
        lambda c: c.execute(_COST_SQL.replace(":window_from", "?"), params).fetchone()
    )
    latency_rows = await store._read(
        lambda c: c.execute(_LATENCY_SQL.replace(":window_from", "?"), params).fetchall()
    )

    latencies: dict[str, list[int]] = {}
    for row in latency_rows:
        latencies.setdefault(str(row["tier"]), []).append(int(row["latency_ms"]))
    for values in latencies.values():
        values.sort()

    tiers = [_tier_usage(row, latencies.get(str(row["tier"]), [])) for row in tier_rows]
    return CostOverviewResponse(
        totals=_totals(tiers, cost_row),
        by_tier=tiers,
        by_task_type=[_task_usage(row) for row in task_rows],
        undeployed_tiers=_undeployed_tiers(settings),
    )


def _tier_usage(row: sqlite3.Row, latencies: list[int]) -> TierUsage:
    get = row.__getitem__
    prompt_tokens = int(get("prompt_tokens") or 0)
    read_tokens = int(get("prompt_cache_read_tokens") or 0)
    call_count = int(get("call_count") or 0)
    cached_count = int(get("cached_count") or 0)
    models = str(get("models") or "")
    return TierUsage(
        tier=str(get("tier")),
        call_count=call_count,
        cached_count=cached_count,
        failed_count=int(get("failed_count") or 0),
        fallback_count=int(get("fallback_count") or 0),
        prompt_cache_read_tokens=read_tokens,
        prompt_cache_write_tokens=int(get("prompt_cache_write_tokens") or 0),
        prompt_cache_read_rate=_rate(read_tokens, prompt_tokens),
        prompt_tokens=prompt_tokens,
        output_tokens=int(get("output_tokens") or 0),
        total_tokens=prompt_tokens + int(get("output_tokens") or 0),
        cache_hit_rate=_rate(cached_count, call_count),
        p50_latency_ms=_percentile(latencies, 0.5),
        p95_latency_ms=_percentile(latencies, 0.95),
        models=sorted({model for model in models.split(",") if model}),
    )


def _task_usage(row: sqlite3.Row) -> TaskTypeUsage:
    get = row.__getitem__
    call_count = int(get("call_count") or 0)
    return TaskTypeUsage(
        task_type=str(get("task_type")),
        tier=str(get("tier")),
        call_count=call_count,
        total_tokens=int(get("total_tokens") or 0),
        cache_hit_rate=_rate(int(get("cached_count") or 0), call_count),
        prompt_cache_read_tokens=int(get("prompt_cache_read_tokens") or 0),
        prompt_cache_write_tokens=int(get("prompt_cache_write_tokens") or 0),
    )


def _totals(tiers: list[TierUsage], cost_row: sqlite3.Row) -> CostTotals:
    get = cost_row.__getitem__
    priced = int(get("priced_count") or 0)
    unpriced = int(get("unpriced_count") or 0)
    call_count = sum(tier.call_count for tier in tiers)
    cached_count = sum(tier.cached_count for tier in tiers)
    read_tokens = sum(tier.prompt_cache_read_tokens for tier in tiers)
    # 只要有一条调用缺单价，总额就是不完整的——标 partial，而不是把缺的按 0 加进去。
    cost_status = (
        _COST_UNAVAILABLE if priced == 0 else (_COST_OK if unpriced == 0 else _COST_PARTIAL)
    )
    return CostTotals(
        call_count=call_count,
        cached_count=cached_count,
        cache_hit_rate=_rate(cached_count, call_count),
        prompt_cache_read_tokens=read_tokens,
        prompt_cache_write_tokens=sum(tier.prompt_cache_write_tokens for tier in tiers),
        prompt_cache_read_rate=_rate(read_tokens, sum(tier.prompt_tokens for tier in tiers)),
        total_tokens=sum(tier.total_tokens for tier in tiers),
        failed_count=sum(tier.failed_count for tier in tiers),
        fallback_count=sum(tier.fallback_count for tier in tiers),
        priced_count=priced,
        unpriced_count=unpriced,
        cost_usd=(
            None if priced == 0 else str(Decimal(int(get("cost_micro_usd") or 0)) / MICRO)
        ),
        cost_status=cost_status,
        window_from=_optional_datetime(get("window_from")),
        window_to=_optional_datetime(get("window_to")),
    )


def _percentile(values: list[int], q: float) -> int | None:
    """离散分位数，与原来的 `percentile_disc` 同义：取真实存在的那个观测值。

    插值出来的 p95 是一个从未发生过的延迟，对"最慢的那次有多慢"这个问题是误导。
    """
    if not values:
        return None
    index = min(len(values) - 1, int(q * len(values)))
    return values[index]


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


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
