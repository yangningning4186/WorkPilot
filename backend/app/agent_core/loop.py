"""Provider-neutral 的模型决策 → 工具执行循环。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

type Node[StateT] = Callable[[StateT], Awaitable[StateT]]


async def run_tool_loop[StateT](
    state: StateT,
    *,
    decide: Node[StateT],
    execute_tools: Node[StateT],
    is_active: Callable[[StateT], bool],
    has_pending_tools: Callable[[StateT], bool],
) -> StateT:
    """运行不包含 Cowork Prompt、权限或具体工具的确定性 Agent 工具循环。

    循环本身不设置固定步数：产品层通过模型调用预算、费用闸门、重复调用熔断、
    用户取消和上下文保护负责收敛，状态与 checkpoint 则由调用方显式持久化。
    """

    current = state
    while is_active(current):
        current = await decide(current)
        if not is_active(current):
            break
        if has_pending_tools(current):
            current = await execute_tools(current)
    return current
