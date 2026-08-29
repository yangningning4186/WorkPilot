"""LLM 调用的测量 schema。

一次调用实际发生了什么——档位、模型、token、延迟、缓存命中、是否降级、成本归属。
`workpilot_ai` 的网关按这个 schema 产出记录，落库实现在 `app/telemetry/llm_calls.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol
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
    cause: Literal["primary", "compaction", "hook", "adjustment"] = "primary"
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
    # ai.request is a child of the currently active agent turn span.
    span_id: str | None = None
    parent_span_id: str | None = None
    stop_reason: Literal["stop", "length", "tool_use", "error"] | None = None


class AuditSink(Protocol):
    async def record(self, call: AuditRecord) -> None: ...


@dataclass(frozen=True)
class AttributeSpec:
    python_types: tuple[type[Any], ...]
    required: bool
    cardinality: Literal["low", "medium", "high"]
    sensitive: bool = False
    allowed_values: tuple[object, ...] = ()


AUDIT_ATTRIBUTE_SCHEMA: dict[str, AttributeSpec] = {
    "trace_id": AttributeSpec((str,), True, "high"),
    "task_type": AttributeSpec((str,), True, "low"),
    "tier": AttributeSpec(
        (str,), True, "low", allowed_values=("light", "main", "heavy", "external")
    ),
    "model": AttributeSpec((str,), True, "low"),
    "provider": AttributeSpec((str,), True, "low"),
    "input_tokens": AttributeSpec((int,), True, "medium"),
    "output_tokens": AttributeSpec((int,), True, "medium"),
    "latency_ms": AttributeSpec((int,), True, "medium"),
    "success": AttributeSpec((bool,), True, "low", allowed_values=(True, False)),
    "cause": AttributeSpec(
        (str,),
        True,
        "low",
        allowed_values=("primary", "compaction", "hook", "adjustment"),
    ),
    "prompt_cache_read_tokens": AttributeSpec((int,), True, "medium"),
    "prompt_cache_write_tokens": AttributeSpec((int,), True, "medium"),
    "cost_usd": AttributeSpec((Decimal,), False, "medium"),
    "cached": AttributeSpec((bool,), True, "low", allowed_values=(True, False)),
    "cache_type": AttributeSpec(
        (str,), False, "low", allowed_values=("exact", "semantic", "prompt")
    ),
    "was_fallback": AttributeSpec((bool,), True, "low", allowed_values=(True, False)),
    "run_id": AttributeSpec((UUID,), False, "high"),
    "eval_run_id": AttributeSpec((UUID,), False, "high"),
    "batch_id": AttributeSpec((UUID,), False, "high"),
    "span_id": AttributeSpec((str,), False, "high"),
    "parent_span_id": AttributeSpec((str,), False, "high"),
    "stop_reason": AttributeSpec(
        (str,), False, "low", allowed_values=("stop", "length", "tool_use", "error")
    ),
}


def validate_audit_record(record: AuditRecord) -> None:
    values = vars(record)
    if set(values) != set(AUDIT_ATTRIBUTE_SCHEMA):
        missing = sorted(set(AUDIT_ATTRIBUTE_SCHEMA) - set(values))
        extra = sorted(set(values) - set(AUDIT_ATTRIBUTE_SCHEMA))
        raise ValueError(f"audit attribute schema 漂移: missing={missing}, extra={extra}")
    for name, spec in AUDIT_ATTRIBUTE_SCHEMA.items():
        value = values[name]
        if value is None:
            if spec.required:
                raise ValueError(f"audit attribute {name} 是必填项")
            continue
        if not isinstance(value, spec.python_types):
            raise TypeError(f"audit attribute {name} 类型错误: {type(value).__name__}")
        if spec.allowed_values and value not in spec.allowed_values:
            raise ValueError(f"audit attribute {name} 值不在封闭集合中: {value!r}")
