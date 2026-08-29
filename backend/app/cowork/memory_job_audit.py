"""自动记忆作业的最小持久审计 envelope。

``process_memory_job_source`` 的内存返回值为了测试与即时日志仍含候选 fact；这里是跨越
持久化边界前的唯一投影，只复制 operation/status/category/scope/reason/IDs。严格契约在
``cowork_contracts.normalize_memory_job_result`` 再校验一次，Store 也会重复校验。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.cowork_contracts import (
    MEMORY_JOB_RESULT_MAX_OPERATIONS,
    MEMORY_JOB_RESULT_REASON_MAX_CHARS,
    MEMORY_JOB_RESULT_SCHEMA,
    normalize_memory_job_result,
)


def _bounded_reason(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()[:MEMORY_JOB_RESULT_REASON_MAX_CHARS]
    # JSON 上限按 UTF-8 bytes 计算。只按字符截断会让 500 个 emoji/汉字把 16 KiB
    # envelope 撑破，导致已经应用完记忆操作的 worker 在结算时反复重试。
    encoded = normalized.encode("utf-8")
    if len(encoded) > MEMORY_JOB_RESULT_REASON_MAX_CHARS:
        normalized = encoded[:MEMORY_JOB_RESULT_REASON_MAX_CHARS].decode("utf-8", errors="ignore")
    return normalized.strip() or None


def build_memory_job_result(operations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """把含候选原文的运行结果投影成无 fact/content 的审计结果。"""

    audit_operations: list[dict[str, Any]] = []
    for raw in operations[:MEMORY_JOB_RESULT_MAX_OPERATIONS]:
        operation = raw.get("operation")
        skipped = operation == "SKIP" or raw.get("skipped") is True
        if skipped:
            operation_status = "skipped"
        elif raw.get("applied") is True:
            operation_status = "applied"
        elif operation == "NOOP" or raw.get("current_changed") is False:
            operation_status = "unchanged"
        else:
            operation_status = "blocked"
        skipped_reason = _bounded_reason(raw.get("skipped_reason"))
        if skipped and skipped_reason is None:
            skipped_reason = _bounded_reason(raw.get("reason")) or "memory_operation_skipped"
        audit_operations.append(
            {
                "operation": operation,
                "requested_operation": raw.get("requested_operation"),
                "status": operation_status,
                "category": raw.get("category"),
                "scope": raw.get("scope"),
                "reason": _bounded_reason(raw.get("reason")),
                "skipped_reason": skipped_reason,
                "target_memory_id": raw.get("target_memory_id"),
                "memory_id": raw.get("memory_id"),
            }
        )
    return normalize_memory_job_result(
        {
            "schema_version": MEMORY_JOB_RESULT_SCHEMA,
            "status": "completed",
            "skipped_reason": None,
            "operations": audit_operations,
            "truncated_operations": max(0, len(operations) - len(audit_operations)),
        }
    )


def build_skipped_memory_job_result(reason: str) -> dict[str, Any]:
    """作业在 provider 前被控制面阻止时的审计结果。"""

    return normalize_memory_job_result(
        {
            "schema_version": MEMORY_JOB_RESULT_SCHEMA,
            "status": "skipped",
            "skipped_reason": _bounded_reason(reason) or "memory_job_skipped",
            "operations": [],
            "truncated_operations": 0,
        }
    )
