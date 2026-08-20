"""GPU 批次的落库部分（口径说明见 :mod:`workpilot_ai.batch`）。

`BatchSpec` 与 `current_batch_id` 是纯契约，留在 `workpilot_ai`；
建表写表需要 `AsyncSession`，因此留在应用层。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import monotonic
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.core.config import Settings
from workpilot_ai.batch import (
    BatchPricingNotConfiguredError,
    BatchSpec,
    batch_scope,
)

logger = structlog.get_logger(__name__)


def batch_spec_from_settings(
    settings: Settings, *, tier: str, model: str, label: str
) -> BatchSpec:
    price = settings.gpu_price_usd_per_hour
    if price <= 0:
        # 默认路径：不做美元折算，只统计 token 与 GPU 时间。
        return BatchSpec(
            tier=tier,
            model=model,
            label=label,
            node_count=settings.gpu_node_count,
            gpu_model=settings.gpu_model.strip() or None,
        )
    if not settings.gpu_price_source.strip():
        raise BatchPricingNotConfiguredError(
            "配置了 GPU_PRICE_USD_PER_HOUR 就必须同时写 GPU_PRICE_SOURCE："
            "写明取值出处（报价页面 + 查询日期）。docs/07 §7.3。"
        )
    return BatchSpec(
        tier=tier,
        model=model,
        label=label,
        node_count=settings.gpu_node_count,
        gpu_model=settings.gpu_model.strip() or None,
        price_usd_per_hour=price,
        price_source=settings.gpu_price_source.strip(),
    )


@asynccontextmanager
async def gpu_batch(session: AsyncSession, spec: BatchSpec) -> AsyncIterator[UUID]:
    """开一个批次，块内所有网关调用共享同一个 `batch_id`。

    墙钟用 `monotonic()` 测：数据库的 `now()` 是事务开始时间，同一事务里写的多条
    调用时间戳完全相同，从 `created_at` 反推批次时长恒等于 0。

    进程被杀时 `ended_at` / `wall_ms` 保持为空，成本报告会整批排除它——
    半个批次的墙钟摊出来的单价没有意义，宁可缺一条数据也不要一条错数据。
    """

    batch_id = uuid7()
    await session.execute(
        text(
            """
            INSERT INTO gpu_batches
                (id, tier, model, label, node_count, gpu_model,
                 price_usd_per_hour, price_source)
            VALUES
                (:id, :tier, :model, :label, :node_count, :gpu_model,
                 :price_usd_per_hour, :price_source)
            """
        ),
        {
            "id": batch_id,
            "tier": spec.tier,
            "model": spec.model,
            "label": spec.label,
            "node_count": spec.node_count,
            "gpu_model": spec.gpu_model,
            "price_usd_per_hour": spec.price_usd_per_hour,
            "price_source": spec.price_source,
        },
    )
    # 先提交, 否则批次内的 llm_calls 外键指向一个还没落库的批次。
    await session.commit()

    started = monotonic()
    try:
        with batch_scope(batch_id):
            yield batch_id
    finally:
        wall_ms = max(0, round((monotonic() - started) * 1000))
        await session.execute(
            text(
                """
                UPDATE gpu_batches
                SET ended_at = clock_timestamp(), wall_ms = :wall_ms
                WHERE id = :id
                """
            ),
            {"id": batch_id, "wall_ms": wall_ms},
        )
        await session.commit()
        logger.info("GPU 批次结束", batch_id=str(batch_id), label=spec.label, wall_ms=wall_ms)
