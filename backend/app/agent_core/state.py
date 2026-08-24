"""Agent 状态的通用形状与序列化（框架层，见 ADR-0011）。

本模块只定义**任何** Agent 都有的东西：计划步骤、人工中断、预算归一化、
JSON 可序列化校验。它不知道任何一个具体工作流的字段——综述的 card/group
在 `app/agent/review_state.py`，Cowork 的在 `cowork_runtime.py`。

约束 2 的落点：状态必须是可 JSON 序列化的 TypedDict。`json_state` 是那条约束的
运行时执行者，任何 checkpoint 落库前都要过它。
"""

from __future__ import annotations

import json
from typing import Any, Literal, TypedDict, cast

from app.agent_core.contracts import BudgetState as BudgetState

PlanStepStatus = Literal["pending", "running", "done", "failed", "skipped"]
PLAN_STEP_STATUSES: frozenset[str] = frozenset({"pending", "running", "done", "failed", "skipped"})


class PlanStepState(TypedDict):
    id: str
    idx: int
    description: str
    tool: str | None
    depends_on: list[int]
    status: PlanStepStatus


class InterruptState(TypedDict):
    kind: Literal["write_confirm"]
    payload: dict[str, object]
    resume_token: str


def normalize_budget[StateT](state: StateT) -> StateT:
    """给早于预算熔断的 checkpoint 补上缺失的预算字段。

    `used_wall_ms` 是在 A0 骨架之后才加的。老 checkpoint 反序列化回来会缺这个键,
    TypedDict 不做运行时校验, 缺键要到第一次累加时才炸。这里显式补 0:
    老 run 的已耗墙钟无法追认, 按 0 起算是唯一诚实的选择, 且只会让预算更宽松而不是更严。
    """

    budget = cast("dict[str, Any]", state)["budget"]
    if "used_wall_ms" not in budget:
        budget["used_wall_ms"] = 0
    return state


def json_state[StateT](state: StateT) -> StateT:
    """拒绝 UUID/datetime/客户端等不可 JSON 化对象，并返回无别名的纯数据副本。"""

    encoded = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # pragma: no cover - Agent 状态根节点固定为对象
        raise TypeError("Agent 状态必须是 JSON object")
    return cast("StateT", decoded)
