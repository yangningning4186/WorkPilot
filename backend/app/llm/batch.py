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
    """一次跑批的硬件口径。

    单价是**可选**的：本项目决定不做美元折算。等价云单价是个外部假设，填不同的数
    会得到不同的"成本"，而"哪个配置在前沿上"这个结论只取决于 token 与 GPU 时间的
    相对关系。与其引入一个不可验证的假设，不如只报可直接测量的量。

    真要填单价时，`price_source` 就变成必填——§7.3 要求报告里写出取值与出处，
    数据库的 check 约束也会挡下没来源的单价。
    """

    tier: str
    model: str
    label: str
    node_count: int = 1
    gpu_model: str | None = None
    price_usd_per_hour: Decimal | None = None
    price_source: str | None = None


class BatchPricingNotConfiguredError(RuntimeError):
    """配了单价却没写来源。

    只在**主动填了单价**时才会抛：没填单价是正常模式（只报 token 与吞吐），
    填了却说不清出处才是问题——§7.3 明确要求报告里写出取值与来源，
    说不清口径的成本数字一问就露馅。
    """


def batch_spec_from_settings(
    settings: "Settings", *, tier: str, model: str, label: str
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
