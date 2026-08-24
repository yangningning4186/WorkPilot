"""Cowork 任务清单：模型自己维护、前端直接渲染的结构化 todo。

和 `agent_plan_steps` 的区别是方向。plan step 是 runtime 从每次 tool call 派生的
**事后日志**（"调用了 read_text_file"），用于溯源；todo 是模型对"这件事分几步、
现在到第几步"的**主动声明**，用于让人看懂进度。两者都要有，互相替代不了。

清单存在 `CoworkState` 里（约束 2：状态必须是可 JSON 序列化的 TypedDict）。工具
handler 只做规范化并把结果交回 runtime，自身不持有任何可变状态——注册表实例上的
可变状态活不过 worker 重启，`_activated_tools` 已经踩过一次。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

TodoStatus = Literal["pending", "in_progress", "done"]
TODO_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "done"})
TODO_TOOL_NAME = "todo_write"

MAX_TODOS = 30
MAX_TODO_CONTENT_CHARS = 200

# 模型很爱自由发挥状态词。这些别名不是猜的：openai/anthropic 的模型普遍把 done 写成
# completed。与其让它吃一个 schema 校验错误再重试一轮（多一次决策调用），不如在入口
# 归一。归一只覆盖语义完全等价的写法，不认识的一律走校验失败，由模型自己纠正。
_STATUS_ALIASES: dict[str, TodoStatus] = {
    "complete": "done",
    "completed": "done",
    "finished": "done",
    "doing": "in_progress",
    "in-progress": "in_progress",
    "inprogress": "in_progress",
    "active": "in_progress",
    "todo": "pending",
    "not_started": "pending",
}


class TodoItem(TypedDict):
    content: str
    status: TodoStatus


class TodoEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_TODO_CONTENT_CHARS)
    status: TodoStatus = "pending"

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().casefold()
            return _STATUS_ALIASES.get(normalized, normalized)
        return value


class TodoWriteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    todos: list[TodoEntry] = Field(max_length=MAX_TODOS)


def todo_items(args: TodoWriteArgs) -> list[TodoItem]:
    return [{"content": entry.content, "status": entry.status} for entry in args.todos]


def normalize_todos(raw: object) -> list[TodoItem]:
    """从 checkpoint JSON 或工具输出里还原清单，逐层判型。

    读到的可能是旧版本 checkpoint、被截断的 JSON 或别的 Agent 写的状态，遇到不合规
    的条目跳过而不是抛错——一份坏掉的清单不该让整个 run 无法恢复。
    """

    if not isinstance(raw, list):
        return []
    items: list[TodoItem] = []
    for entry in raw[:MAX_TODOS]:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content")
        status = entry.get("status")
        if not isinstance(content, str) or not content.strip():
            continue
        if not isinstance(status, str) or status not in TODO_STATUSES:
            status = "pending"
        items.append(
            {
                "content": content[:MAX_TODO_CONTENT_CHARS],
                "status": cast("TodoStatus", status),
            }
        )
    return items


def todo_summary(todos: list[TodoItem]) -> dict[str, Any]:
    """给事件与工具结果用的计数摘要，前端不必自己数。"""

    return {
        "total": len(todos),
        "done": sum(1 for item in todos if item["status"] == "done"),
        "in_progress": sum(1 for item in todos if item["status"] == "in_progress"),
        "pending": sum(1 for item in todos if item["status"] == "pending"),
    }


def render_todo_block(todos: list[TodoItem]) -> str:
    """把当前清单钉进 system prompt。

    只靠历史里的 todo_write tool call 是不够的：压缩会把它归档，模型随后就忘了自己
    定过什么计划。清单本身很短，钉在压缩边界之上的代价可以忽略。
    """

    if not todos:
        return ""
    marks = {"done": "x", "in_progress": ">", "pending": " "}
    lines = "\n".join(f"[{marks[item['status']]}] {item['content']}" for item in todos)
    return (
        "<current_todos>\n"
        "这是你自己通过 todo_write 维护的任务清单，是当前进度的唯一事实来源。\n"
        "只有动作已有成功结果且必要验证通过，才能标为 done；计划、发起调用或口头说明都不算完成。\n"
        "完成一项就重发完整清单更新状态，同一时刻恰好一项 in_progress；不要只在正文里口头更新。\n"
        f"{lines}\n"
        "</current_todos>"
    )
