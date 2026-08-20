"""按批 GPU wall time 摊销的成本报告（docs/07 §7.2–7.3）。

§7.3 的要求是：**只报"每次问答 ¥0.003"是不够的**，因为它强依赖当时的并发度。
同一个模型在并发 1 和并发 16 下的单位成本可以差一个数量级。所以每个批次都要
同时给出成本、吞吐、并发度和占用率，缺一个数字都没法解释。
"""

from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from workpilot_telemetry.cost import _QUANTUM, _SECONDS_PER_HOUR
from workpilot_telemetry.cost import BatchCost as BatchCost


async def load_batch_costs(
    session: AsyncSession, *, label: str | None = None, batch_id: UUID | None = None
) -> list[BatchCost]:
    """按批聚合并摊销。

    只统计**已收尾**的批次（`wall_ms IS NOT NULL`）：进程中途被杀的批次拿不到
    完整墙钟，用半个墙钟摊出来的单价是错的，宁可缺一条也不要错一条。

    **按 label 查会撞车。** label 是人给的名字，重跑同一个实验就会出现同名批次，
    取第一条会拿到上一次（可能是被中断的那次）的数据，而且看不出来。程序化取用
    一律传 `batch_id`；`label` 只用于人看报告时的过滤。

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
                  AND (CAST(:batch_id AS UUID) IS NULL OR b.id = CAST(:batch_id AS UUID))
                GROUP BY b.id
                ORDER BY b.started_at
                """
            ),
            {"label": label, "batch_id": batch_id},
        )
    ).mappings()

    results: list[BatchCost] = []
    for row in rows:
        wall_s = (Decimal(row["wall_ms"]) / 1000).quantize(Decimal("0.001"))
        price = row["price_usd_per_hour"]
        gpu_cost = (
            None
            if price is None
            else (Decimal(price) / _SECONDS_PER_HOUR * wall_s * row["node_count"]).quantize(
                _QUANTUM, rounding=ROUND_HALF_UP
            )
        )
        results.append(
            BatchCost(
                batch_id=row["id"],
                label=row["label"],
                tier=row["tier"],
                model=row["model"],
                gpu_model=row["gpu_model"],
                node_count=row["node_count"],
                price_usd_per_hour=None if price is None else Decimal(price),
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
    """给人看的报告。

    成本的**主口径是 GPU 秒与 token**，不是美元：美元只是 GPU 秒乘一个外部假设
    （等价云单价），而"哪个配置在前沿上"这个结论对那个常数不敏感。配了单价才多两列。
    吞吐与并发度必须同时出现——单任务成本强依赖当时的并发度，缺了它没法解释（§7.3）。
    """

    if not batches:
        return "没有已收尾的批次。"
    priced = [item for item in batches if item.gpu_cost_usd is not None]
    header = (
        f"{'label':<20}{'tier':<8}{'任务':>5}{'墙钟s':>9}{'并发':>7}{'占用':>7}"
        f"{'task/s':>9}{'tok/s':>10}{'GPUs/任务':>11}{'tok/任务':>10}"
    )
    if priced:
        header += f"{'$/任务':>11}{'$/ktok':>11}"
    lines = [header, "-" * len(header)]
    for item in batches:
        row = (
            f"{item.label[:19]:<20}{item.tier:<8}{item.task_count:>5}"
            f"{item.wall_s:>9}{item.mean_concurrency:>7}{item.client_occupancy:>7}"
            f"{item.tasks_per_s:>9}{item.tokens_per_s:>10}"
            f"{item.gpu_s_per_task:>11}{item.tokens_per_task:>10}"
        )
        if priced:
            per_task = item.cost_per_task_usd
            per_ktok = item.cost_per_ktok_usd
            row += f"{'—' if per_task is None else per_task:>11}"
            row += f"{'—' if per_ktok is None else per_ktok:>11}"
        lines.append(row)
    lines.append("")
    gpus = {f"{item.gpu_model or '未标注'}×{item.node_count}" for item in batches}
    lines.append(f"硬件口径：{', '.join(sorted(gpus))}")
    if priced:
        sources = {item.price_source for item in priced if item.price_source}
        lines.append(f"单价来源：{'; '.join(sorted(sources))}")
    else:
        lines.append("成本口径：GPU 秒 × 节点数与 token，未做美元折算（等价云单价是外部假设）。")
    lines.append(
        "占用率是客户端观测的端点占用（并发/节点数），不是 GPU SM 利用率——"
        "后者需要在推理机上采 nvidia-smi。"
    )
    return "\n".join(lines)
