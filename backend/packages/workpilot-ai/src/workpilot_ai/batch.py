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

本模块只保留**口径与上下文**。建表写表需要 `AsyncSession`，那部分是应用层的
落库适配器，见 :mod:`app.telemetry.gpu_batches`。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

# contextvar 而不是参数透传: 一次跑批里嵌套着检索、门控、生成好几层调用,
# 逐层加参数会污染每一个业务函数的签名, 而它们本来不该知道成本口径的存在。
_current_batch: ContextVar[UUID | None] = ContextVar("gpu_batch_id", default=None)


def current_batch_id() -> UUID | None:
    return _current_batch.get()


@contextmanager
def batch_scope(batch_id: UUID) -> Iterator[None]:
    """把 `batch_id` 绑到当前上下文；块内所有网关调用都会带上它。

    落库适配器需要 set/reset 这个 contextvar，但不该去碰模块私有变量，
    所以这里给出唯一的公开入口。
    """

    token = _current_batch.set(batch_id)
    try:
        yield
    finally:
        _current_batch.reset(token)


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

