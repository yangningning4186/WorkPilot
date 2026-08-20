"""成本看板读模型（docs/07 §7.3–7.4）。

这组用例守的是看板**不许骗人**的三条：
缺价格不写成 0、缓存命中不撑大摊销分母、未部署的档位不显示成"没人用"。
"""

from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.core.db import get_db_session
from app.llm.audit import SqlLlmCallAudit
from app.llm.batch import BatchSpec, gpu_batch
from app.llm.types import AuditRecord
from app.main import create_app
from app.services.cost_overview import _batch_summary, _totals, get_cost_overview
from app.services.cost_report import BatchCost


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
        batch_id=batch_id,  # type: ignore[arg-type]
    )


def _batch(
    *, price: str | None, task_count: int = 4, wall_s: str = "10", label: str = "T"
) -> BatchCost:
    gpu_cost = (
        None if price is None else (Decimal(price) / 3600 * Decimal(wall_s)).quantize(
            Decimal("0.000001")
        )
    )
    return BatchCost(
        batch_id=uuid7(),
        label=label,
        tier="main",
        model="m",
        gpu_model="A100-80G",
        node_count=1,
        price_usd_per_hour=None if price is None else Decimal(price),
        price_source=None if price is None else "测试固定值",
        wall_s=Decimal(wall_s),
        task_count=task_count,
        total_tokens=10_000,
        output_tokens=5_000,
        busy_s=Decimal("8"),
        gpu_cost_usd=gpu_cost,
    )


# ----------------------------------------------------- 缺价格不许写成 0（§7.4）


def test_missing_price_reports_null_not_zero() -> None:
    """"没有可用价格"和"测过、就是不要钱"是两回事，看板必须分得开。"""
    summary = _batch_summary(_batch(price=None))

    assert summary is not None
    assert summary.cost_usd is None
    assert summary.cost_per_task_usd is None
    assert summary.cost_status == "unavailable"
    assert summary.cost_reason is not None
    # 最容易犯的错就是这个：把 None 渲染成 "0.00" 之后没人再看得出区别
    assert summary.cost_usd != "0.00"


def test_partial_pricing_is_not_summed_as_zero() -> None:
    """一部分批次缺价时总额标 partial，缺的那部分不按 0 加进去。"""
    priced = _batch_summary(_batch(price="3.60"))
    unpriced = _batch_summary(_batch(price=None))
    assert priced is not None and unpriced is not None

    totals = _totals([], [priced, unpriced], window_from=None, window_to=None)

    assert totals.cost_status == "partial"
    assert totals.priced_batch_count == 1
    assert totals.unpriced_batch_count == 1
    assert totals.cost_usd == str(Decimal(priced.cost_usd or "0"))


def test_all_unpriced_totals_stay_unavailable() -> None:
    unpriced = _batch_summary(_batch(price=None))
    assert unpriced is not None

    totals = _totals([], [unpriced], window_from=None, window_to=None)

    assert totals.cost_status == "unavailable"
    assert totals.cost_usd is None


def test_batch_without_billable_calls_is_hidden() -> None:
    """0 个计费调用时每一项单位成本的分母都是 0，摆上看板只会误导。"""
    assert _batch_summary(_batch(price="3.60", task_count=0)) is None


# ------------------------------------------------------- 真实数据库上的聚合


async def test_cache_hits_count_for_hit_rate_but_not_for_tokens(
    db_session: AsyncSession,
) -> None:
    """命中不消耗 GPU：进命中率的分子，不进 token 分母。

    算进 token 会把摊销分母撑大、单任务成本虚低——那正好把缓存收益重复计了一遍。
    """
    audit = SqlLlmCallAudit(db_session)
    await audit.record(_record(input_tokens=900, output_tokens=100))
    await audit.record(_record(cached=True, input_tokens=900, output_tokens=100))

    overview = await get_cost_overview(db_session, settings=_Settings())  # type: ignore[arg-type]

    main = next(tier for tier in overview.by_tier if tier.tier == "main")
    assert main.call_count == 2
    assert main.cached_count == 1
    assert main.cache_hit_rate == 0.5
    # 只有未命中的那次计 token
    assert main.total_tokens == 1000


async def test_failures_and_fallbacks_are_counted_separately(
    db_session: AsyncSession,
) -> None:
    audit = SqlLlmCallAudit(db_session)
    await audit.record(_record())
    await audit.record(_record(success=False))
    await audit.record(_record(was_fallback=True))

    overview = await get_cost_overview(db_session, settings=_Settings())  # type: ignore[arg-type]

    assert overview.totals.failed_count == 1
    assert overview.totals.fallback_count == 1
    assert overview.totals.call_count == 3


async def test_provider_prompt_cache_tokens_are_reported_separately(
    db_session: AsyncSession,
) -> None:
    audit = SqlLlmCallAudit(db_session)
    await audit.record(
        _record(
            input_tokens=1_000,
            output_tokens=100,
            prompt_cache_read_tokens=750,
            prompt_cache_write_tokens=200,
        )
    )

    overview = await get_cost_overview(db_session, settings=_Settings())  # type: ignore[arg-type]

    main = next(tier for tier in overview.by_tier if tier.tier == "main")
    assert main.prompt_cache_read_tokens == 750
    assert main.prompt_cache_write_tokens == 200
    assert main.prompt_cache_read_rate == 0.75
    # Prompt cache 仍执行了模型，不得冒充零成本的 exact cache hit。
    assert main.cached_count == 0
    assert main.total_tokens == 1_100


async def test_task_type_breakdown_shows_which_tier_answered(
    db_session: AsyncSession,
) -> None:
    """light 未部署时，路由到 light 的任务实际落在 main —— 这张表要看得出来。"""
    audit = SqlLlmCallAudit(db_session)
    await audit.record(_record(task_type="evidence_sufficiency", tier="main"))
    await audit.record(_record(task_type="grounded_answer", tier="main"))

    overview = await get_cost_overview(db_session, settings=_Settings())  # type: ignore[arg-type]

    pairs = {(row.task_type, row.tier) for row in overview.by_task_type}
    assert ("evidence_sufficiency", "main") in pairs
    assert ("grounded_answer", "main") in pairs


async def test_batch_costs_appear_with_throughput_and_concurrency(
    db_session: AsyncSession,
) -> None:
    """§7.3：成本、吞吐、并发度、占用率必须同时出现，缺一个就没法解释这个数字。"""
    label = f"overview-{uuid7()}"
    audit = SqlLlmCallAudit(db_session)
    async with gpu_batch(db_session, BatchSpec(tier="main", model="m", label=label)) as bid:
        await audit.record(_record(batch_id=bid))
        await audit.record(_record(batch_id=bid))

    overview = await get_cost_overview(db_session, settings=_Settings(), label=label)  # type: ignore[arg-type]

    assert len(overview.batches) == 1
    batch = overview.batches[0]
    assert batch.task_count == 2
    for field in (batch.gpu_s, batch.tasks_per_s, batch.mean_concurrency, batch.client_occupancy):
        assert field  # 四个数一个都不能缺
    assert batch.cost_status == "unavailable"  # 本机自部署没有单价


async def test_overview_endpoint_requires_admin(db_session: AsyncSession) -> None:
    """成本页暴露的是运营信息——档位分布、GPU 单价与来源、跑批标签。

    资料库页挂 demo session 是因为它是产品的一部分；成本不是，演示时也不该被看到。
    """

    async def override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/cost/overview")

    assert response.status_code == 401


async def test_window_excludes_older_calls(db_session: AsyncSession) -> None:
    audit = SqlLlmCallAudit(db_session)
    await audit.record(_record())
    await db_session.execute(
        text("UPDATE llm_calls SET created_at = now() - interval '60 days'")
    )

    overview = await get_cost_overview(db_session, settings=_Settings(), days=30)  # type: ignore[arg-type]

    assert overview.totals.call_count == 0
