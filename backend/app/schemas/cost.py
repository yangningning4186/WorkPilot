"""成本看板的对外契约（docs/07 §7.3）。

金额一律用 `str | None` 而不是 float：
本机自部署价格表是 0，"没有可用价格"和"测过、就是不要钱"是两回事，
所以缺价格时给 `null` 并附 `cost_status`，绝不写成 `0.00`（docs/07 §7.4）。
"""

from datetime import datetime

from pydantic import BaseModel


class TierUsage(BaseModel):
    """按档位聚合的调用画像。"""

    tier: str
    call_count: int
    cached_count: int
    failed_count: int
    fallback_count: int
    prompt_tokens: int
    output_tokens: int
    total_tokens: int
    # 命中的调用不消耗 GPU，成本记 0；命中率是缓存收益的唯一直接证据
    cache_hit_rate: float
    p50_latency_ms: int | None
    p95_latency_ms: int | None
    models: list[str]


class TaskTypeUsage(BaseModel):
    task_type: str
    tier: str
    call_count: int
    total_tokens: int
    cache_hit_rate: float


class BatchSummary(BaseModel):
    """一次跑批的摊销结果。

    §7.3 的硬要求：成本、吞吐、并发度、占用率四个数必须同时出现。
    只报"每次问答 ¥0.003"没有意义——同一个模型在并发 1 与并发 16 下能差一个数量级。
    """

    batch_id: str
    label: str
    tier: str
    model: str
    gpu_model: str | None
    node_count: int
    task_count: int
    total_tokens: int
    output_tokens: int
    wall_s: str
    gpu_s: str
    gpu_s_per_task: str
    tokens_per_task: int
    tasks_per_s: str
    tokens_per_s: str
    mean_concurrency: str
    # 客户端观测的占用率，**不是 GPU SM 利用率**；本项目没有 nvidia-smi 采集链路
    client_occupancy: str
    price_usd_per_hour: str | None
    price_source: str | None
    cost_usd: str | None
    cost_per_task_usd: str | None
    cost_per_ktok_usd: str | None
    cost_status: str
    cost_reason: str | None


class CostTotals(BaseModel):
    call_count: int
    cached_count: int
    cache_hit_rate: float
    total_tokens: int
    failed_count: int
    fallback_count: int
    batch_count: int
    # 有价格的批次才计入金额；没有价格的批次数单列，避免把"缺价"读成"免费"
    priced_batch_count: int
    unpriced_batch_count: int
    cost_usd: str | None
    cost_status: str
    window_from: datetime | None
    window_to: datetime | None


class CostOverviewResponse(BaseModel):
    totals: CostTotals
    by_tier: list[TierUsage]
    by_task_type: list[TaskTypeUsage]
    batches: list[BatchSummary]
    # 未部署或未接入的档位：路由表里声明了但线上落不到，看板要说清楚而不是显示 0 调用
    undeployed_tiers: list[str]
