"""GPU 批次上下文（docs/07 §7.2）。

自建模型没有账单，成本按**整批 GPU wall time 摊销**：

    批次 GPU 成本 = (等价云单价 / 3600) × 批次墙钟秒数 × 节点数
    单任务成本   = 批次 GPU 成本 / 批次内任务数

§7.1 点名的错误算法是 `latency × 单价 × 节点数`：8 个请求并发跑在同一块卡上，
每个都记"我占了 2 秒 GPU"，加起来 16 秒，实际只花了 2 秒。同一段 GPU 时间被
重复计费 8 次，而且**并发越高错得越离谱**——它会让"提高并发"这个真正的优化
在账面上显示为成本上升。

**只有跑批才有批次。** 线上单条问答不是批次：给它打 batch_id 会把整段 GPU 墙钟
摊到一次调用上，算出一个荒谬的数字。所以 `batch_id` 在线上是 NULL，成本报告
也只覆盖显式开了批次的调用。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from time import monotonic
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

if TYPE_CHECKING:
    from app.core.config import Settings

logger = structlog.get_logger(__name__)

# contextvar 而不是参数透传: 一次跑批里嵌套着检索、门控、生成好几层调用,
# 逐层加参数会污染每一个业务函数的签名, 而它们本来不该知道成本口径的存在。
_current_batch: ContextVar[UUID | None] = ContextVar("gpu_batch_id", default=None)


def current_batch_id() -> UUID | None:
    return _current_batch.get()


@dataclass(frozen=True)
class BatchSpec:
    """一次跑批的硬件与计价口径。

    `price_source` 必填且必须是能追溯的出处（§7.3：报告里要写出取值与来源）。
    留空会被数据库的 check 约束挡下来。
    """

    tier: str
    model: str
    label: str
    gpu_model: str
    price_usd_per_hour: Decimal
    price_source: str
    node_count: int = 1


class BatchPricingNotConfiguredError(RuntimeError):
    """没配等价云单价就开批次。

    是硬失败而不是默认 0：单价为 0 会让所有成本数字变成 0，报告照样能生成、
    图照样能画，但每一个数字都是错的，且从结果里看不出来。
    """


def batch_spec_from_settings(
    settings: "Settings", *, tier: str, model: str, label: str
) -> BatchSpec:
    if settings.gpu_price_usd_per_hour <= 0 or not settings.gpu_price_source.strip():
        raise BatchPricingNotConfiguredError(
            "开跑批前必须配置等价云 GPU 单价与来源：在 .env 里设置 "
            "GPU_MODEL、GPU_PRICE_USD_PER_HOUR、GPU_PRICE_SOURCE（写明取值出处，"
            "例如某云 A100-80G 按需实例页面与查询日期）、GPU_NODE_COUNT。"
            "docs/07 §7.3 要求报告里写出单价取值与来源。"
        )
    if not settings.gpu_model.strip():
        raise BatchPricingNotConfiguredError("开跑批前必须配置 GPU_MODEL（等价云实例规格）")
    return BatchSpec(
        tier=tier,
        model=model,
        label=label,
        gpu_model=settings.gpu_model.strip(),
        price_usd_per_hour=settings.gpu_price_usd_per_hour,
        price_source=settings.gpu_price_source.strip(),
        node_count=settings.gpu_node_count,
    )


@asynccontextmanager
async def gpu_batch(
    session: AsyncSession, spec: BatchSpec
) -> AsyncIterator[UUID]:
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

    token = _current_batch.set(batch_id)
    started = monotonic()
    try:
        yield batch_id
    finally:
        _current_batch.reset(token)
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
