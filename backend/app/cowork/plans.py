"""计划模式：先出方案、经用户批准，再动手。

要解决的是一类具体的失败——目标含糊时模型会一边猜一边写，等用户看到产出，文件已经
改完了。计划模式把"猜"提前到一个可以廉价推翻的位置：调研阶段只放行只读工具，模型必须
用 `propose_plan` 把打算怎么做讲清楚，运行在那里暂停，用户批准之后才解锁写入类工具。

**批准是运行时状态的翻转，不是 prompt 里的一句约定。** `CoworkState["mode"]` 从
`plan` 变成 `execute` 之前，写工具既不会出现在下发目录里，也不会通过执行边界的检查。
提示词只负责让模型知道自己处于哪个阶段，越权时的兜底不依赖它。

批准后计划会直接变成 todo 清单（见 `app/cowork/todos.py`）。计划如果只以一条 assistant
消息的形式留在历史里，压缩一次就没了，模型会忘掉自己承诺过什么；变成清单才会被钉在
压缩边界之上。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.cowork.todos import TodoItem

CoworkMode = Literal["plan", "execute"]
COWORK_MODES: frozenset[str] = frozenset({"plan", "execute"})
PLAN_TOOL_NAME = "propose_plan"

MAX_PLAN_STEPS = 12
MAX_PLAN_STEP_CHARS = 200
MAX_PLAN_SUMMARY_CHARS = 400
MAX_PLAN_NOTES_CHARS = 1000

PlanStepText = Annotated[str, StringConstraints(min_length=1, max_length=MAX_PLAN_STEP_CHARS)]


class ProposePlanArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=MAX_PLAN_SUMMARY_CHARS)
    steps: list[PlanStepText] = Field(min_length=1, max_length=MAX_PLAN_STEPS)
    notes: str | None = Field(default=None, max_length=MAX_PLAN_NOTES_CHARS)


def normalize_mode(value: object) -> CoworkMode:
    """从 checkpoint JSON 还原模式，认不出来一律当 execute。

    保守方向是执行而不是计划：老 checkpoint 里没有这个字段，把它们判成计划模式会让
    正在跑的任务突然被写工具拦住，而且没有任何人会来批准一个它从未提交的计划。
    """

    if isinstance(value, str) and value in COWORK_MODES:
        return cast("CoworkMode", value)
    return "execute"


def plan_steps(request: Mapping[str, Any]) -> list[str]:
    """从 inbox 里存下来的计划请求还原步骤文本。"""

    raw = request.get("steps")
    if not isinstance(raw, list):
        return []
    steps: list[str] = []
    for entry in raw[:MAX_PLAN_STEPS]:
        if isinstance(entry, str) and entry.strip():
            steps.append(entry.strip()[:MAX_PLAN_STEP_CHARS])
    return steps


def plan_todos(steps: list[str]) -> list[TodoItem]:
    """批准的计划即清单，用户看到的执行进度和他批的那份是同一份。"""

    return [{"content": step, "status": "pending"} for step in steps]


def render_plan_mode_block() -> str:
    """计划阶段钉在 system prompt 里的行为约定。

    只写模型该怎么做；越权的实际拦截在 runtime 与注册表的执行边界上，不靠这段文字。
    """

    return (
        "<plan_mode>\n"
        "当前处于计划模式：可以用只读工具调研，但不能写文件、跑命令或触发外部动作。\n"
        "先查清现状、依赖、风险和验收方式；不要用计划掩盖本可通过只读工具消除的未知项。\n"
        "调研到足以说清做法时调用 propose_plan，步骤写成可验证的结果，不要只列工具名。运行会暂停等用户批准；\n"
        "批准之后写入类工具才会解锁，批准的步骤会直接成为你的任务清单。\n"
        "propose_plan 必须是该轮唯一工具调用；提交后停止。用户不批准时据其意见重新调研或提交，不要提前动手。\n"
        "纯问答或一步就能答完的只读请求直接回答即可，不必提计划。\n"
        "</plan_mode>"
    )
