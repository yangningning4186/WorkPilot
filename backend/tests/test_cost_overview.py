"""成本看板读模型（docs/07 §7.3–7.4）。

这组用例守的是看板**不许骗人**的三条：
缺价格不写成 0、缓存命中不撑大摊销分母、未部署的档位不显示成"没人用"。
"""

from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from uuid6 import uuid7

from app.main import create_app
from app.telemetry.cost_overview import get_cost_overview
from app.telemetry.sqlite import SqliteTelemetryStore
from workpilot_ai.types import AuditRecord


@pytest.fixture
async def store(tmp_path: Path) -> SqliteTelemetryStore:
    created = SqliteTelemetryStore(tmp_path / "telemetry.db")
    await created.initialize()
    return created


class _Settings:
    """只提供 `_undeployed_tiers` 需要的那一个字段。"""

    def __init__(self, routing_path: str = "/nonexistent/routing.yaml") -> None:
        from pathlib import Path

        self.routing_config_path = Path(routing_path)


def _record(
    *,
    tier: str = "main",
    task_type: str = "grounded_answer",
    cached: bool = False,
    success: bool = True,
    was_fallback: bool = False,
    input_tokens: int = 900,
    output_tokens: int = 100,
    latency_ms: int = 1000,
    prompt_cache_read_tokens: int = 0,
    prompt_cache_write_tokens: int = 0,
    cost_usd: Decimal | None = None,
    batch_id: object = None,
) -> AuditRecord:
    return AuditRecord(
        trace_id=str(uuid7()),
        task_type=task_type,
        tier=tier,  # type: ignore[arg-type]
        model="qwen3.6-35b-a3b",
        provider="openai_compatible",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        success=success,
        prompt_cache_read_tokens=prompt_cache_read_tokens,
        prompt_cache_write_tokens=prompt_cache_write_tokens,
        cached=cached,
        cache_type="exact" if cached else None,
        was_fallback=was_fallback,
        cost_usd=cost_usd,
        batch_id=batch_id,  # type: ignore[arg-type]
    )


# ---------------------------------------------- 缺价格不许写成 0（§7.4）


async def test_all_unpriced_totals_stay_unavailable(store: SqliteTelemetryStore) -> None:
    """本机自部署没有单价：金额必须是 null + unavailable，不能是 0.00。

    折成 0 之后，"没测过价格"和"测过、就是不要钱"在页面上再也分不开——而这两件事
    对"该不该换个更便宜的档"给出的结论完全相反。
    """
    await store.record(_record())
    await store.record(_record())

    totals = (await get_cost_overview(store, settings=_Settings())).totals  # type: ignore[arg-type]

    assert totals.cost_usd is None
    assert totals.cost_status == "unavailable"
    assert (totals.priced_count, totals.unpriced_count) == (0, 2)


async def test_partial_pricing_is_not_summed_as_zero(store: SqliteTelemetryStore) -> None:
    """一半有价一半没有：总额只能标 partial，不能把没价的按 0 加进去。"""
    await store.record(_record(cost_usd=Decimal("0.250000")))
    await store.record(_record())

    totals = (await get_cost_overview(store, settings=_Settings())).totals  # type: ignore[arg-type]

    assert totals.cost_status == "partial"
    assert totals.cost_usd == "0.25"
    assert (totals.priced_count, totals.unpriced_count) == (1, 1)


async def test_fully_priced_totals_are_ok(store: SqliteTelemetryStore) -> None:
    await store.record(_record(cost_usd=Decimal("0.100000")))
    await store.record(_record(cost_usd=Decimal("0.200000")))

    totals = (await get_cost_overview(store, settings=_Settings())).totals  # type: ignore[arg-type]

    assert totals.cost_status == "ok"
    assert totals.cost_usd == "0.3"
    assert totals.unpriced_count == 0


async def test_cache_hits_count_for_hit_rate_but_not_for_tokens(
    store: SqliteTelemetryStore,
) -> None:
    """命中不消耗 GPU：进命中率的分子，不进 token 分母。

    算进 token 会把摊销分母撑大、单任务成本虚低——那正好把缓存收益重复计了一遍。
    """
    audit = store
    await audit.record(_record(input_tokens=900, output_tokens=100))
    await audit.record(_record(cached=True, input_tokens=900, output_tokens=100))

    overview = await get_cost_overview(store, settings=_Settings())  # type: ignore[arg-type]

    main = next(tier for tier in overview.by_tier if tier.tier == "main")
    assert main.call_count == 2
    assert main.cached_count == 1
    assert main.cache_hit_rate == 0.5
    # 只有未命中的那次计 token
    assert main.total_tokens == 1000


async def test_failures_and_fallbacks_are_counted_separately(
    store: SqliteTelemetryStore,
) -> None:
    audit = store
    await audit.record(_record())
    await audit.record(_record(success=False))
    await audit.record(_record(was_fallback=True))

    overview = await get_cost_overview(store, settings=_Settings())  # type: ignore[arg-type]

    assert overview.totals.failed_count == 1
    assert overview.totals.fallback_count == 1
    assert overview.totals.call_count == 3


async def test_provider_prompt_cache_tokens_are_reported_separately(
    store: SqliteTelemetryStore,
) -> None:
    audit = store
    await audit.record(
        _record(
            input_tokens=1_000,
            output_tokens=100,
            prompt_cache_read_tokens=750,
            prompt_cache_write_tokens=200,
        )
    )

    overview = await get_cost_overview(store, settings=_Settings())  # type: ignore[arg-type]

    main = next(tier for tier in overview.by_tier if tier.tier == "main")
    assert main.prompt_cache_read_tokens == 750
    assert main.prompt_cache_write_tokens == 200
    assert main.prompt_cache_read_rate == 0.75
    # Prompt cache 仍执行了模型，不得冒充零成本的 exact cache hit。
    assert main.cached_count == 0
    assert main.total_tokens == 1_100


async def test_task_type_breakdown_shows_which_tier_answered(
    store: SqliteTelemetryStore,
) -> None:
    """light 未部署时，路由到 light 的任务实际落在 main —— 这张表要看得出来。"""
    audit = store
    await audit.record(_record(task_type="evidence_sufficiency", tier="main"))
    await audit.record(_record(task_type="grounded_answer", tier="main"))

    overview = await get_cost_overview(store, settings=_Settings())  # type: ignore[arg-type]

    pairs = {(row.task_type, row.tier) for row in overview.by_task_type}
    assert ("evidence_sufficiency", "main") in pairs
    assert ("grounded_answer", "main") in pairs


async def test_overview_endpoint_requires_admin() -> None:
    """成本页暴露的是运营信息——档位分布、模型与用量画像，必须要求 owner 登录。"""

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/cost/overview")

    assert response.status_code == 401


async def test_window_excludes_older_calls(store: SqliteTelemetryStore) -> None:
    await store.record(_record())
    await store._write(
        lambda c: c.execute("UPDATE llm_calls SET created_at = datetime('now', '-60 days')")
    )

    overview = await get_cost_overview(store, settings=_Settings(), days=30)  # type: ignore[arg-type]

    assert overview.totals.call_count == 0
