"""按批 GPU wall time 摊销的成本口径（docs/07 §7.2–7.3）。

§7.3 的要求是：**只报"每次问答 ¥0.003"是不够的**，因为它强依赖当时的并发度。
同一个模型在并发 1 和并发 16 下的单位成本可以差一个数量级。所以每个批次都要
同时给出成本、吞吐、并发度和占用率，缺一个数字都没法解释。

这里只有纯 Decimal 计算，没有一行 SQL——口径必须能脱离表结构被单独读懂和验证。
从数据库把 `BatchCost` 装出来的部分在 `app/telemetry/cost_report.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

_SECONDS_PER_HOUR = Decimal(3600)
_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True)
class BatchCost:
    batch_id: UUID
    label: str
    tier: str
    model: str
    gpu_model: str | None
    node_count: int
    price_usd_per_hour: Decimal | None
    price_source: str | None
    wall_s: Decimal
    task_count: int
    total_tokens: int
    output_tokens: int
    busy_s: Decimal
    gpu_cost_usd: Decimal | None

    @property
    def gpu_s(self) -> Decimal:
        """摊到本批的 GPU 秒数 = 墙钟 × 节点数。

        不配单价时这就是成本本身。美元只是它乘一个常数，
        而"哪个配置在前沿上"的结论对这个常数不敏感。
        """

        return (self.wall_s * self.node_count).quantize(Decimal("0.001"))

    @property
    def gpu_s_per_task(self) -> Decimal:
        if self.task_count == 0:
            return Decimal(0)
        return (self.gpu_s / self.task_count).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    @property
    def tokens_per_task(self) -> int:
        if self.task_count == 0:
            return 0
        return round(self.total_tokens / self.task_count)

    @property
    def cost_per_task_usd(self) -> Decimal | None:
        if self.gpu_cost_usd is None or self.task_count == 0:
            return None
        return (self.gpu_cost_usd / self.task_count).quantize(_QUANTUM, rounding=ROUND_HALF_UP)

    @property
    def cost_per_ktok_usd(self) -> Decimal | None:
        """按总 token（prompt + output）计。

        两种口径都给是 §7.3 的明确要求：每任务成本便于跟商用 API 的"一次问答多少钱"
        对齐，每千 token 成本便于跨任务长度比较。只给一种就没法横向比。
        """

        if self.gpu_cost_usd is None or self.total_tokens == 0:
            return None
        return (self.gpu_cost_usd / (Decimal(self.total_tokens) / 1000)).quantize(
            _QUANTUM, rounding=ROUND_HALF_UP
        )

    @property
    def tasks_per_s(self) -> Decimal:
        if self.wall_s == 0:
            return Decimal(0)
        return (Decimal(self.task_count) / self.wall_s).quantize(
            Decimal("0.001"), rounding=ROUND_HALF_UP
        )

    @property
    def tokens_per_s(self) -> Decimal:
        if self.wall_s == 0:
            return Decimal(0)
        return (Decimal(self.total_tokens) / self.wall_s).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )

    @property
    def mean_concurrency(self) -> Decimal:
        """平均并发 = 所有调用的忙碌时间之和 / 批次墙钟。

        这是成本数字的解释变量：并发 1 时单任务成本约等于"一个人独占整台机器"，
        并发 16 时才是真实的摊销收益。报成本不报它等于没报（§7.3）。
        """

        if self.wall_s == 0:
            return Decimal(0)
        return (self.busy_s / self.wall_s).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def client_occupancy(self) -> Decimal:
        """客户端观测的占用率 = 平均并发 / 节点数，上限不封顶。

        **这不是 GPU SM 利用率。** 它只回答"这批跑批把端点喂饱了没有"，
        从客户端能测到的也只有这个。真正的 SM 利用率要在推理机上采 `nvidia-smi`，
        本项目没有那条采集链路——所以这一列必须叫占用率，不能冒充利用率。
        §7.3 要的"利用率 30% 时的成本数字没有参考价值"这层判断，用它足够了。
        """

        if self.node_count == 0:
            return Decimal(0)
        return (self.mean_concurrency / self.node_count).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
