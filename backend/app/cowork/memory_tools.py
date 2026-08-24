"""模型写入长期记忆的四个入口。

`remember` 存新事实；`memory_update` / `memory_forget` 按注入块里的 `[#id]` 改写或
retire 旧事实——**修正要替换而不是在旁边堆一条**，否则同一件事的新旧两个版本会同时
出现在上下文里，模型没有办法判断哪个还算数。`memory_read` 取被截断记忆的全文。

这四个工具都是 `effect="none"`：记忆写的是 WorkPilot 自己的控制面，不是用户的文件或
外部系统，不需要目录授权，也不进 `tool_invocations` 租约。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.cowork.memory import (
    MAX_MEMORY_CONTENT_CHARS,
    MAX_MEMORY_KEY_CHARS,
    default_workspace_path,
    forget_memory,
    get_memory,
    memory_payload,
    remember,
    update_memory,
)
from app.cowork.tools import (
    CoworkToolContext,
    CoworkToolError,
    CoworkToolRegistry,
    CoworkToolResult,
    CoworkToolSpec,
)
from app.cowork_contracts import MemoryNotFoundError, MemoryScope, MemoryScopeError


class RememberArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_MEMORY_CONTENT_CHARS)
    scope: MemoryScope = "global"
    key: str | None = Field(default=None, max_length=MAX_MEMORY_KEY_CHARS)


class MemoryUpdateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: UUID
    content: str = Field(min_length=1, max_length=MAX_MEMORY_CONTENT_CHARS)


class MemoryIdArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: UUID


async def _remember(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = RememberArgs.model_validate(raw.model_dump())
    workspace_path = (
        await default_workspace_path(context.session, conversation_id=context.conversation_id)
        if args.scope == "workspace"
        else None
    )
    try:
        record, previous = await remember(
            context.session,
            conversation_id=context.conversation_id,
            scope=args.scope,
            content=args.content,
            key=args.key,
            workspace_path=workspace_path,
            source="agent",
        )
    except MemoryScopeError as error:
        raise CoworkToolError(str(error)) from error
    return CoworkToolResult(
        output={
            "memory": memory_payload(record),
            "replaced": previous is not None,
            "previous_content": None if previous is None else previous.content,
        }
    )


async def _memory_update(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = MemoryUpdateArgs.model_validate(raw.model_dump())
    try:
        record, previous = await update_memory(
            context.session, memory_id=args.memory_id, content=args.content
        )
    except MemoryNotFoundError as error:
        raise CoworkToolError(
            f"记忆 {args.memory_id} 不存在，请使用 known_memories 里给出的 [#id]"
        ) from error
    except MemoryScopeError as error:
        raise CoworkToolError(str(error)) from error
    return CoworkToolResult(
        output={"memory": memory_payload(record), "previous_content": previous.content}
    )


async def _memory_forget(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = MemoryIdArgs.model_validate(raw.model_dump())
    record = await forget_memory(context.session, memory_id=args.memory_id)
    if record is None:
        # 已经删过或从来不存在，都不是需要模型补救的错误。
        return CoworkToolResult(
            output={"memory_id": str(args.memory_id), "already_forgotten": True}
        )
    return CoworkToolResult(output={"memory": memory_payload(record), "already_forgotten": False})


async def _memory_read(context: CoworkToolContext, raw: BaseModel) -> CoworkToolResult:
    args = MemoryIdArgs.model_validate(raw.model_dump())
    record = await get_memory(context.session, memory_id=args.memory_id)
    if record is None or record.forgotten_at is not None:
        raise CoworkToolError(f"记忆 {args.memory_id} 不存在或已被 retire")
    # 只读自己作用域内可见的记忆；别的会话的私有记忆不该被 id 猜到。
    if record.scope == "conversation" and record.conversation_id != context.conversation_id:
        raise CoworkToolError(f"记忆 {args.memory_id} 不属于当前会话")
    return CoworkToolResult(output={"memory": memory_payload(record)})


def register_memory_tools(registry: CoworkToolRegistry) -> None:
    registry.register_deferred(
        CoworkToolSpec(
            name="remember",
            description=(
                "记住一条跨会话有效的长期事实，例如用户偏好或项目约定。"
                "scope 取 global（对所有会话有效）、workspace（只对当前工作目录有效）"
                "或 conversation（只对本次会话有效）。"
                "同一件事请带上稳定的 key，这样后续会更新同一条而不是再堆一条。"
                "只记会长期成立的结论，不要记一次性的中间结果。"
            ),
            args_model=RememberArgs,
            risk="read",
            effect="none",
            parallel_safe=False,
            handler=_remember,
            search_aliases=("memory", "记忆", "记住", "偏好"),
        ),
        group="记忆",
    )
    registry.register_deferred(
        CoworkToolSpec(
            name="memory_update",
            description=(
                "改写一条已有记忆。发现 known_memories 里的事实过时了要用它替换，"
                "不要用 remember 在旁边补一条新的。memory_id 用注入块里的 [#id]。"
            ),
            args_model=MemoryUpdateArgs,
            risk="read",
            effect="none",
            parallel_safe=False,
            handler=_memory_update,
            search_aliases=("memory", "记忆", "更正"),
        ),
        group="记忆",
    )
    registry.register_deferred(
        CoworkToolSpec(
            name="memory_forget",
            description="retire 一条不再成立的记忆。用户明确说不要再记着某件事时也用它。",
            args_model=MemoryIdArgs,
            risk="read",
            effect="none",
            parallel_safe=False,
            handler=_memory_forget,
            search_aliases=("memory", "记忆", "忘记"),
        ),
        group="记忆",
    )
    registry.register_deferred(
        CoworkToolSpec(
            name="memory_read",
            description="取一条记忆的完整内容；注入块里标注已截断时用它补全。",
            args_model=MemoryIdArgs,
            risk="read",
            effect="none",
            parallel_safe=True,
            handler=_memory_read,
            search_aliases=("memory", "记忆"),
        ),
        group="记忆",
    )
    registry.add_system_instructions(
        "用户表达长期偏好、项目约定或纠正你的既有认知时，调用 remember 记下来；"
        "已有记忆过时用 memory_update 替换、用 memory_forget retire，不要新旧并存。"
        "一次性的中间结果不要写进记忆。"
    )
