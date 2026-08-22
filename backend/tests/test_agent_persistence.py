"""约束 2：Agent 状态必须是可 JSON 序列化的。

原来这份文件测的是固定综述的 state 持久化。那条 workflow 退役后，仍然值得钉住的只剩
框架层这一条——它是断点续跑与时间旅行调试的地基，而且失败方式很隐蔽：把连接、客户端、
UUID 塞进 state 不会当场报错，只会在存 checkpoint 的那一刻炸，或者更糟，静默存成一个
恢复不回来的东西。
"""

from uuid import uuid4

import pytest
from uuid6 import uuid7

from app.agent_core.state import PlanStepState, json_state


def _plan() -> list[PlanStepState]:
    return [
        {
            "id": str(uuid7()),
            "idx": 0,
            "description": "筛选文档",
            "tool": "list_documents",
            "depends_on": [],
            "status": "pending",
        },
    ]


def test_agent_state_rejects_non_json_runtime_objects() -> None:
    state = {"plan": _plan(), "artifacts": {"bad": uuid4()}}

    with pytest.raises(TypeError):
        json_state(state)  # type: ignore[arg-type]


def test_agent_state_round_trips_a_plain_plan() -> None:
    """反过来也要成立，否则上面那条用"什么都拒绝"也能通过。"""
    state = {"plan": _plan(), "artifacts": {"note": "reviews/memory.md"}}

    assert json_state(state) == state  # type: ignore[arg-type]
