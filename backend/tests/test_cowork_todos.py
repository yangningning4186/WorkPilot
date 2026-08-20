from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.core.config import get_settings
from app.cowork.runtime import _system_prompt
from app.cowork.todos import (
    MAX_TODO_CONTENT_CHARS,
    TODO_TOOL_NAME,
    TodoItem,
    TodoWriteArgs,
    normalize_todos,
    render_todo_block,
    todo_items,
    todo_summary,
)
from app.cowork.tools import CoworkToolContext, build_default_cowork_registry


def _args(raw: dict[str, Any]) -> TodoWriteArgs:
    return TodoWriteArgs.model_validate(raw)


def test_status_aliases_are_normalized() -> None:
    """模型普遍把 done 写成 completed；归一比让它吃一次校验错误再重试便宜。"""

    args = _args(
        {
            "todos": [
                {"content": "读取源文件", "status": "completed"},
                {"content": "生成报告", "status": "In-Progress"},
                {"content": "复核", "status": "not_started"},
            ]
        }
    )

    assert [item["status"] for item in todo_items(args)] == [
        "done",
        "in_progress",
        "pending",
    ]


def test_unknown_status_is_rejected_not_silently_coerced() -> None:
    """认识的别名才归一；看不懂的状态要让模型自己纠正，不能猜。"""

    with pytest.raises(ValidationError):
        _args({"todos": [{"content": "读取源文件", "status": "blocked"}]})


def test_status_defaults_to_pending_and_extra_keys_are_rejected() -> None:
    assert todo_items(_args({"todos": [{"content": "读取源文件"}]})) == [
        {"content": "读取源文件", "status": "pending"}
    ]
    with pytest.raises(ValidationError):
        _args({"todos": [{"content": "读取", "status": "pending", "id": 3}]})


def test_normalize_todos_survives_broken_checkpoint_payloads() -> None:
    """一份坏掉的清单不该让整个 run 无法从 checkpoint 恢复。"""

    assert normalize_todos(None) == []
    assert normalize_todos("not a list") == []
    assert normalize_todos(
        [
            {"content": "保留", "status": "done"},
            {"content": "状态不认识", "status": "blocked"},
            {"content": "   "},
            {"status": "pending"},
            "裸字符串",
            {"content": "x" * (MAX_TODO_CONTENT_CHARS + 50), "status": "pending"},
        ]
    ) == [
        {"content": "保留", "status": "done"},
        {"content": "状态不认识", "status": "pending"},
        {"content": "x" * MAX_TODO_CONTENT_CHARS, "status": "pending"},
    ]


def test_todo_summary_counts_each_status() -> None:
    todos: list[TodoItem] = [
        {"content": "a", "status": "done"},
        {"content": "b", "status": "in_progress"},
        {"content": "c", "status": "pending"},
        {"content": "d", "status": "pending"},
    ]

    assert todo_summary(todos) == {
        "total": 4,
        "done": 1,
        "in_progress": 1,
        "pending": 2,
    }


def test_todo_block_is_pinned_into_system_prompt() -> None:
    """清单要钉在压缩边界之上，否则压缩一次模型就忘了自己定过什么计划。"""

    todos: list[TodoItem] = [
        {"content": "读取源文件", "status": "done"},
        {"content": "生成报告", "status": "in_progress"},
        {"content": "复核", "status": "pending"},
    ]

    assert render_todo_block([]) == ""
    prompt = _system_prompt("扩展说明", todos=todos)
    assert "<current_todos>" in prompt
    assert "[x] 读取源文件" in prompt
    assert "[>] 生成报告" in prompt
    assert "[ ] 复核" in prompt
    # 空清单不占位，也不该在没有 todo 的会话里凭空多出一段。
    assert "<current_todos>" not in _system_prompt("扩展说明")


async def test_todo_write_is_a_pure_read_tool_and_echoes_the_list() -> None:
    """handler 不持有可变状态：清单只经 output 交回 runtime，由 checkpoint 保存。"""

    registry = build_default_cowork_registry()
    spec = registry.get(TODO_TOOL_NAME)
    assert spec.effect == "none"
    assert spec.risk == "read"
    assert spec.capability is None
    assert spec.approval_required is False
    # 两次并行写会争夺同一份状态，必须串行。
    assert spec.parallel_safe is False

    result = await registry.execute(
        TODO_TOOL_NAME,
        {
            "todos": [
                {"content": "读取源文件", "status": "done"},
                {"content": "生成报告", "status": "in_progress"},
            ]
        },
        context=CoworkToolContext(
            session=cast("Any", None),
            gateway=cast("Any", None),
            settings=get_settings(),
            conversation_id=UUID(int=1),
            run_id=UUID(int=2),
            worker_id="todo-worker",
            plan_step_id=UUID(int=3),
            tool_call_id="todo-call",
        ),
    )

    assert result.effect_ref is None
    assert result.output["todos"] == [
        {"content": "读取源文件", "status": "done"},
        {"content": "生成报告", "status": "in_progress"},
    ]
    assert result.output["total"] == 2
    assert result.output["done"] == 1


def test_todo_write_is_always_in_the_core_catalog() -> None:
    """只在某些话题下才出现的计划工具没有意义。"""

    registry = build_default_cowork_registry()

    for query in ("整理本地文件", "搜一下新闻", "写个 shell 脚本", ""):
        names = {item.name for item in registry.tool_definitions_for(query)}
        assert TODO_TOOL_NAME in names
