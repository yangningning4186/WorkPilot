"""统一授权回执：解释一次工具调用为什么被安全边界允许。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.agent_core.idempotency import canonical_json


def arguments_sha256(arguments: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(arguments)).encode("utf-8")).hexdigest()


def build_authorization_receipt(
    *,
    conversation_id: UUID,
    run_id: UUID,
    plan_step_id: UUID,
    tool_call_id: str,
    tool: str,
    arguments: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    approval: Mapping[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 1,
        "conversation_id": str(conversation_id),
        "run_id": str(run_id),
        "plan_step_id": str(plan_step_id),
        "tool_call_id": tool_call_id,
        "tool": tool,
        "arguments_sha256": arguments_sha256(arguments),
        "decisions": [dict(item) for item in decisions],
        "approval": dict(approval),
    }
    receipt_id = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return {
        **payload,
        "receipt_id": receipt_id,
        "issued_at": datetime.now(UTC).isoformat(),
    }


def compact_receipt_json(receipt: Mapping[str, Any]) -> str:
    return json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ["arguments_sha256", "build_authorization_receipt", "compact_receipt_json"]
