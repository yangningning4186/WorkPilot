"""Provider-neutral 的模型决策 → 工具执行循环。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Hashable
from typing import Any, cast

from langgraph.graph import END, START, StateGraph

type Node[StateT] = Callable[[StateT], Awaitable[StateT]]


async def run_tool_loop[StateT](
    state: StateT,
    *,
    state_schema: type[Any],
    decide: Node[StateT],
    execute_tools: Node[StateT],
    is_active: Callable[[StateT], bool],
    has_pending_tools: Callable[[StateT], bool],
    recursion_limit: int,
) -> StateT:
    """运行一个不包含任何 Cowork Prompt、权限或具体工具的通用 Agent loop。"""

    if recursion_limit < 1:
        raise ValueError("recursion_limit 必须为正数")
    # LangGraph 的类型参数要求在静态分析期拿到具体 TypedDict；这里刻意接受由
    # 产品层传入的 schema，因此把 builder 边界收敛为 Any，外部仍保持 StateT。
    builder: Any = StateGraph(state_schema)
    builder.add_node("decide", decide)
    builder.add_node("tool", execute_tools)
    builder.add_edge(START, "decide")

    # LangGraph 会在运行期 get_type_hints(route)；PEP 695 的函数局部 StateT 不在
    # 模块 globals 中，因此这里使用 Any，并在闭包边界恢复泛型类型。
    def route(current: Any) -> str:
        typed = cast("StateT", current)
        if not is_active(typed):
            return END
        return "tool" if has_pending_tools(typed) else "decide"

    destinations: dict[Hashable, str] = {"tool": "tool", "decide": "decide", END: END}
    builder.add_conditional_edges("decide", route, destinations)
    builder.add_edge("tool", "decide")
    graph = builder.compile()
    return cast(
        StateT,
        await graph.ainvoke(state, config={"recursion_limit": recursion_limit}),
    )
