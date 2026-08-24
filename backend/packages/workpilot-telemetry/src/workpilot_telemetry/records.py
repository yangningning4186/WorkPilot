"""LLM 调用的测量 schema。

一次调用实际发生了什么——档位、模型、token、延迟、缓存命中、是否降级、成本归属。
`workpilot_ai` 的网关按这个 schema 产出记录，落库实现在 `app/telemetry/llm_calls.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True)
class AuditRecord:
    trace_id: str
    task_type: str
    tier: Literal["light", "main", "heavy", "external"]
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    success: bool
    prompt_cache_read_tokens: int = 0
    prompt_cache_write_tokens: int = 0
    cost_usd: Decimal | None = None
    # 缓存命中与 fallback 都是"这次调用实际发生了什么"的一部分。列在 M0 就建好了,
    # 一直没人写——看板要算命中率与降级率(docs/07 §9)就得靠这三列。
    cached: bool = False
    cache_type: Literal["exact", "semantic", "prompt"] | None = None
    was_fallback: bool = False
    run_id: UUID | None = None
    # 评测跑批的归属。与 run_id 是两张表的外键(agent_runs / eval_runs), 不能混用:
    # 评测跑批的逐条 token 与成本就是靠它归集的。
    eval_run_id: UUID | None = None
    # 同批并发调用共享一次 GPU 计时(docs/07 §7.2)。线上单条问答不是批次, 保持 NULL。
    batch_id: UUID | None = None


class AuditSink(Protocol):
    async def record(self, call: AuditRecord) -> None: ...
