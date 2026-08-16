"""按批 GPU wall time 摊销（docs/07 §7）。

这组用例的核心是**证明摊销口径确实避开了 §7.1 点名的错误算法**：
`latency × 单价 × 节点数` 在并发下会把同一段 GPU 时间重复计费，并发越高错得越离谱，
讽刺的是它会让"提高并发"这个真正的优化在账面上显示为成本上升。
"""

from decimal import Decimal
from uuid import UUID

import pytest

from app.llm.batch import (
    BatchPricingNotConfiguredError,
    BatchSpec,
    batch_spec_from_settings,
    current_batch_id,
)
from app.services.cost_report import BatchCost, format_batch_costs


def _cost(
    *,
    wall_s: str,
    task_count: int,
    busy_s: str,
    price: str = "3.60",
    nodes: int = 1,
    total_tokens: int = 10_000,
) -> BatchCost:
    """price 默认取 3.60 USD/h —— 正好每秒 0.001 美元，手算校验一目了然。"""

    gpu_cost = Decimal(price) / 3600 * Decimal(wall_s) * nodes
    return BatchCost(
        batch_id=UUID(int=1),
        label="T",
        tier="main",
        model="m",
        gpu_model="A100-80G",
        node_count=nodes,
        price_usd_per_hour=Decimal(price),
        price_source="测试固定值",
        wall_s=Decimal(wall_s),
        task_count=task_count,
        total_tokens=total_tokens,
        output_tokens=total_tokens // 2,
        busy_s=Decimal(busy_s),
        gpu_cost_usd=gpu_cost.quantize(Decimal("0.000001")),
    )


def test_amortized_cost_is_wall_time_not_summed_latency() -> None:
    """8 个请求并发跑 2 秒：整批只该记 2 秒 GPU，不是 16 秒。

    这就是 §7.1 的错误算法与 §7.2 的正确算法之间的差距，本例里正好差 8 倍。
    """

    batch = _cost(wall_s="2", task_count=8, busy_s="16")

    # 正确口径：2 秒 × 0.001 $/s = 0.002 美元，摊到 8 个任务。
    assert batch.gpu_cost_usd == Decimal("0.002000")
    assert batch.cost_per_task_usd == Decimal("0.000250")

    # 错误口径（latency 求和）会算出 0.016，是真实值的 8 倍。
    wrong = (batch.busy_s * Decimal("0.001")).quantize(Decimal("0.000001"))
    assert wrong == Decimal("0.016000")
    assert wrong == batch.gpu_cost_usd * 8


def test_higher_concurrency_lowers_cost_per_task() -> None:
    """同样的活儿并发跑完，单任务成本必须**下降**。

    错误口径下它会上升——那会让人得出"别提高并发"的荒谬结论。
    """

    serial = _cost(wall_s="16", task_count=8, busy_s="16")
    concurrent = _cost(wall_s="2", task_count=8, busy_s="16")

    assert concurrent.cost_per_task_usd < serial.cost_per_task_usd
    assert concurrent.mean_concurrency == Decimal("8.00")
    assert serial.mean_concurrency == Decimal("1.00")


def test_the_four_required_numbers_are_all_available() -> None:
    """§7.3：只报单任务成本是不够的，它强依赖当时的并发度。"""

    batch = _cost(wall_s="4", task_count=8, busy_s="16", total_tokens=8_000)

    assert batch.cost_per_task_usd > 0
    assert batch.cost_per_ktok_usd > 0
    assert batch.tasks_per_s == Decimal("2.000")
    assert batch.tokens_per_s == Decimal("2000.0")
    assert batch.mean_concurrency == Decimal("4.00")


def test_occupancy_divides_by_node_count() -> None:
    """并发 8 跑在 2 个节点上，每个节点的占用是 4，不是 8。"""

    batch = _cost(wall_s="2", task_count=8, busy_s="16", nodes=2)

    assert batch.mean_concurrency == Decimal("8.00")
    assert batch.client_occupancy == Decimal("4.00")
    # 节点数进成本：两个节点跑两秒，花的是两份 GPU 时间。
    assert batch.gpu_cost_usd == Decimal("0.004000")


def test_empty_batch_does_not_divide_by_zero() -> None:
    batch = _cost(wall_s="0", task_count=0, busy_s="0", total_tokens=0)

    assert batch.cost_per_task_usd == Decimal(0)
    assert batch.cost_per_ktok_usd == Decimal(0)
    assert batch.tasks_per_s == Decimal(0)
    assert batch.mean_concurrency == Decimal(0)
    assert batch.client_occupancy == Decimal(0)


def test_report_states_what_the_occupancy_column_is_not() -> None:
    """占用率不是 GPU SM 利用率。报告里必须说清楚，否则就是冒充测量。"""

    text = format_batch_costs([_cost(wall_s="2", task_count=8, busy_s="16")])

    assert "nvidia-smi" in text
    assert "不是 GPU SM 利用率" in text
    # 单价来源必须出现在报告里（§7.3）。
    assert "测试固定值" in text
    assert "A100-80G×1" in text


def test_empty_report_says_so_instead_of_printing_zeros() -> None:
    assert format_batch_costs([]) == "没有已收尾的批次。"


# ------------------------------------------------------------------ 配置校验


class _Settings:
    gpu_model = ""
    gpu_price_usd_per_hour = Decimal("0")
    gpu_price_source = ""
    gpu_node_count = 1


def test_missing_price_is_a_hard_failure_not_a_zero() -> None:
    """单价缺省成 0 的话，报告照样能生成，但每个数字都是错的且看不出来。"""

    settings = _Settings()

    with pytest.raises(BatchPricingNotConfiguredError, match="GPU_PRICE_USD_PER_HOUR"):
        batch_spec_from_settings(settings, tier="main", model="m", label="C1")  # type: ignore[arg-type]


def test_price_without_a_source_is_rejected() -> None:
    """§7.3 要求写出取值与来源；说不清口径的成本数字一问就露馅。"""

    settings = _Settings()
    settings.gpu_price_usd_per_hour = Decimal("3.60")
    settings.gpu_model = "A100-80G"

    with pytest.raises(BatchPricingNotConfiguredError, match="GPU_PRICE_SOURCE"):
        batch_spec_from_settings(settings, tier="main", model="m", label="C1")  # type: ignore[arg-type]


def test_complete_settings_produce_a_spec() -> None:
    settings = _Settings()
    settings.gpu_price_usd_per_hour = Decimal("3.60")
    settings.gpu_model = "A100-80G"
    settings.gpu_price_source = "某云按需实例页面 2026-08-16 查询"
    settings.gpu_node_count = 2

    spec = batch_spec_from_settings(settings, tier="heavy", model="m", label="C1")  # type: ignore[arg-type]

    assert isinstance(spec, BatchSpec)
    assert spec.node_count == 2
    assert spec.price_source.startswith("某云")


def test_no_batch_context_means_no_batch_id() -> None:
    """线上单条问答不是批次：给它打 batch_id 会把整段墙钟摊到一次调用上。"""

    assert current_batch_id() is None
