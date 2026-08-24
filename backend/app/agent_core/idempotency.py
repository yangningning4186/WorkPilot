"""副作用幂等身份的纯函数与共享错误。"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID


class InvocationInFlightError(RuntimeError):
    """相同副作用仍由另一个未过期租约持有。"""


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def invocation_identity(
    *,
    run_id: UUID,
    plan_step_id: UUID,
    tool_name: str,
    args: dict[str, Any],
) -> tuple[str, str]:
    canonical_args = canonical_json(args)
    args_hash = hashlib.sha256(canonical_args.encode()).hexdigest()
    identity = canonical_json(
        {
            "run_id": str(run_id),
            "plan_step_id": str(plan_step_id),
            "tool_name": tool_name,
            "args": args,
        }
    )
    return hashlib.sha256(identity.encode()).hexdigest(), args_hash
