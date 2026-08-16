"""按批 GPU wall time 摊销的成本报告（docs/07 §7.2–7.3）。

§7.3 的要求是：**只报"每次问答 ¥0.003"是不够的**，因为它强依赖当时的并发度。
同一个模型在并发 1 和并发 16 下的单位成本可以差一个数量级。所以每个批次都要
同时给出成本、吞吐、并发度和占用率，缺一个数字都没法解释。
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_SECONDS_PER_HOUR = Decimal(3600)
_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True)
class BatchCost:
    batch_id: UUID
    label: str
    tier: str
    model: str
    gpu_model: str
    node_count: int
    price_usd_per_hour: Decimal
    price_source: str
    wall_s: Decimal
    task_count: int
    total_tokens: int
    output_tokens: int
    busy_s: Decimal
    gpu_cost_usd: Decimal

    @property
    def cost_per_task_usd(self) -> Decimal:
        if self.task_count == 0:
            return Decimal(0)
        return (self.gpu_cost_usd / self.task_count).quantize(_QUANTUM, rounding=ROUND_HALF_UP)

    @property
    def cost_per_ktok_usd(self) -> Decimal:
        """按总 token（prompt + output）计。

        两种口径都给是 §7.3 的明确要求：每任务成本便于跟商用 API 的"一次问答多少钱"
        对齐，每千 token 成本便于跨任务长度比较。只给一种就没法横向比。
        """

        if self.total_tokens == 0:
            return Decimal(0)
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


async def load_batch_costs(session: AsyncSession, *, label: str | None = None) -> list[BatchCost]:
    """按批聚合并摊销。

    只统计**已收尾**的批次（`wall_ms IS NOT NULL`）：进程中途被杀的批次拿不到
    完整墙钟，用半个墙钟摊出来的单价是错的，宁可缺一条也不要错一条。

    缓存命中不计入任务数与 token：它们没有消耗 GPU 时间，算进去会把摊销分母
    撑大、单任务成本虚低——那正好把缓存的收益重复计算了一遍。
    """

    rows = (
        await session.execute(
            text(
                """
                SELECT
                    b.id, b.label, b.tier, b.model, b.gpu_model, b.node_count,
                    b.price_usd_per_hour, b.price_source, b.wall_ms,
                    COUNT(c.id) FILTER (WHERE NOT c.cached)              AS task_count,
                    COALESCE(SUM(c.prompt_tokens + c.output_tokens)
                             FILTER (WHERE NOT c.cached), 0)             AS total_tokens,
                    COALESCE(SUM(c.output_tokens)
                             FILTER (WHERE NOT c.cached), 0)             AS output_tokens,
                    COALESCE(SUM(c.latency_ms)
                             FILTER (WHERE NOT c.cached), 0)             AS busy_ms
                FROM gpu_batches b
                LEFT JOIN llm_calls c ON c.batch_id = b.id
                -- 显式 CAST：asyncpg 推不出裸参数与 NULL 比较时的类型，
                -- 会抛 AmbiguousParameterError。
                WHERE b.wall_ms IS NOT NULL
                  AND (CAST(:label AS TEXT) IS NULL OR b.label = CAST(:label AS TEXT))
                GROUP BY b.id
                ORDER BY b.started_at
                """
            ),
            {"label": label},
        )
    ).mappings()

    results: list[BatchCost] = []
    for row in rows:
        wall_s = (Decimal(row["wall_ms"]) / 1000).quantize(Decimal("0.001"))
        gpu_cost = (
            Decimal(row["price_usd_per_hour"]) / _SECONDS_PER_HOUR * wall_s * row["node_count"]
        ).quantize(_QUANTUM, rounding=ROUND_HALF_UP)
        results.append(
            BatchCost(
                batch_id=row["id"],
                label=row["label"],
                tier=row["tier"],
                model=row["model"],
                gpu_model=row["gpu_model"],
                node_count=row["node_count"],
                price_usd_per_hour=Decimal(row["price_usd_per_hour"]),
                price_source=row["price_source"],
                wall_s=wall_s,
                task_count=row["task_count"],
                total_tokens=row["total_tokens"],
                output_tokens=row["output_tokens"],
                busy_s=(Decimal(row["busy_ms"]) / 1000).quantize(Decimal("0.001")),
                gpu_cost_usd=gpu_cost,
            )
        )
    return results


def format_batch_costs(batches: list[BatchCost]) -> str:
    """给人看的报告。四个数必须同时出现，缺一个就没法解释成本（§7.3）。"""

    if not batches:
        return "没有已收尾的批次。"
    lines = [
        f"{'label':<16}{'tier':<8}{'任务':>5}{'墙钟s':>9}{'并发':>7}{'占用':>7}"
        f"{'task/s':>9}{'tok/s':>10}{'$/任务':>11}{'$/ktok':>11}",
        "-" * 100,
    ]
    for item in batches:
        lines.append(
            f"{item.label[:15]:<16}{item.tier:<8}{item.task_count:>5}"
            f"{item.wall_s:>9}{item.mean_concurrency:>7}{item.client_occupancy:>7}"
            f"{item.tasks_per_s:>9}{item.tokens_per_s:>10}"
            f"{item.cost_per_task_usd:>11}{item.cost_per_ktok_usd:>11}"
        )
    sources = {item.price_source for item in batches}
    gpus = {f"{item.gpu_model}×{item.node_count}" for item in batches}
    lines.append("")
    lines.append(f"硬件口径：{', '.join(sorted(gpus))}")
    lines.append(f"单价来源：{'; '.join(sorted(sources))}")
    lines.append(
        "占用率是客户端观测的端点占用（并发/节点数），不是 GPU SM 利用率——"
        "后者需要在推理机上采 nvidia-smi。"
    )
    return "\n".join(lines)
