from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.core.db import DbSession as AsyncSession
from app.cowork.memory import (
    forget_memory,
    list_memories,
    load_visible_memories,
    memory_payload,
    remember,
    render_memory_block,
    resolve_binding,
    update_memory,
)
from app.cowork.permissions import create_session_root
from app.cowork.runtime import _ephemeral_context, _system_prompt
from app.cowork_contracts import (
    CoworkMemoryRecord,
    MemoryNotFoundError,
    MemoryScopeError,
)
from app.runstore.runs import ensure_conversation


def _record(content: str, *, scope: str = "global", forgotten: bool = False) -> CoworkMemoryRecord:
    now = datetime.now(UTC)
    return CoworkMemoryRecord(
        id=uuid4(),
        scope=scope,  # type: ignore[arg-type]
        conversation_id=None,
        workspace_path=None,
        key=None,
        content=content,
        source="agent",
        created_at=now,
        updated_at=now,
        forgotten_at=now if forgotten else None,
    )


def test_scope_binding_is_derived_not_supplied() -> None:
    """作用域和定位字段必须一一对应，写歪了检索就会漏掉或串作用域。"""

    conversation_id = UUID(int=7)
    assert resolve_binding("global", conversation_id=conversation_id, workspace_path="/x") == (
        None,
        None,
    )
    assert resolve_binding(
        "conversation", conversation_id=conversation_id, workspace_path="/x"
    ) == (conversation_id, None)
    assert resolve_binding("workspace", conversation_id=conversation_id, workspace_path="/x") == (
        None,
        "/x",
    )
    with pytest.raises(MemoryScopeError):
        resolve_binding("workspace", conversation_id=conversation_id, workspace_path=None)


def test_memory_block_truncates_long_items_and_drops_the_overflow() -> None:
    long_one = _record("长" * 500)
    block = render_memory_block([long_one], max_chars=4000, preview_chars=60)

    assert "长" * 60 in block
    assert "长" * 61 not in block
    assert "memory_read" in block
    assert f"[#{long_one.id}]" in block

    # 总长超限时丢最久没更新的；一条都塞不下就整块不出现。
    many = [_record(f"事实 {index}") for index in range(50)]
    bounded = render_memory_block(many, max_chars=400, preview_chars=240)
    assert len(bounded) <= 400
    assert render_memory_block(many, max_chars=80, preview_chars=240) == ""


def test_forgotten_memories_are_never_injected() -> None:
    block = render_memory_block(
        [_record("已经 retire", forgotten=True)], max_chars=4000, preview_chars=240
    )

    assert block == ""
    assert render_memory_block([], max_chars=4000, preview_chars=240) == ""


def test_memory_lives_in_the_stable_prefix_and_todos_do_not() -> None:
    """记忆是 run 内不变的知识，进 system prompt；清单每轮都在变，只能挂末尾。

    分界不是主题而是"这一次 run 里会不会变"：会变的东西放进 system prompt，模型每动一次
    清单就要把整段前缀重新计费。
    """

    block = render_memory_block([_record("用户偏好 Markdown")], max_chars=4000, preview_chars=240)
    prompt = _system_prompt("", memory_block=block)
    tail = _ephemeral_context(mode="execute", todos=[{"content": "生成报告", "status": "pending"}])

    assert "<known_memories>" in prompt
    assert "<current_todos>" not in prompt
    assert "<current_todos>" in tail
    assert "<known_memories>" not in tail
    assert "<known_memories>" not in _system_prompt("")


async def test_keyed_remember_updates_instead_of_piling_up(db_session: AsyncSession) -> None:
    """同 key 的修正必须替换：新旧并存会让模型无从判断哪个还算数。"""

    conversation_id = await ensure_conversation(db_session, title="Memory upsert")

    first, replaced = await remember(
        db_session,
        conversation_id=conversation_id,
        scope="global",
        content="用户偏好 PDF 报告",
        key="report-format",
    )
    assert replaced is None

    second, previous = await remember(
        db_session,
        conversation_id=conversation_id,
        scope="global",
        content="用户偏好 Markdown 报告",
        key="report-format",
    )

    assert second.id == first.id
    assert previous is not None
    assert previous.content == "用户偏好 PDF 报告"
    visible = await load_visible_memories(db_session, conversation_id=conversation_id)
    assert [item.content for item in visible] == ["用户偏好 Markdown 报告"]


async def test_forget_is_soft_and_idempotent_and_restore_brings_it_back(
    db_session: AsyncSession,
) -> None:
    """撤销依赖软删除；硬删掉就没有第二次机会了。"""

    conversation_id = await ensure_conversation(db_session, title="Memory forget")
    record, _ = await remember(
        db_session,
        conversation_id=conversation_id,
        scope="global",
        content="临时结论",
    )

    forgotten = await forget_memory(db_session, memory_id=record.id)
    assert forgotten is not None and forgotten.forgotten_at is not None
    # 重复删除是幂等的，不是错误。
    assert await forget_memory(db_session, memory_id=record.id) is None
    assert await load_visible_memories(db_session, conversation_id=conversation_id) == []

    restored, _ = await update_memory(db_session, memory_id=record.id, restore=True)
    assert restored.forgotten_at is None
    assert [
        item.id for item in await load_visible_memories(db_session, conversation_id=conversation_id)
    ] == [record.id]

    with pytest.raises(MemoryNotFoundError):
        await update_memory(db_session, memory_id=uuid4(), content="不存在")


async def test_visibility_never_leaks_across_conversations_or_workspaces(
    db_session: AsyncSession, tmp_path
) -> None:
    """作用域漏了就是把无关事实喂给模型，所以按可见性而不是按 id 过滤。"""

    mine = await ensure_conversation(db_session, title="Memory mine")
    theirs = await ensure_conversation(db_session, title="Memory theirs")
    my_root = tmp_path / "mine"
    other_root = tmp_path / "other"
    my_root.mkdir()
    other_root.mkdir()
    await create_session_root(
        db_session,
        conversation_id=mine,
        requested_path=str(my_root),
        access_mode="read_write",
    )

    await remember(db_session, conversation_id=mine, scope="global", content="全局偏好")
    await remember(db_session, conversation_id=mine, scope="conversation", content="本会话笔记")
    await remember(db_session, conversation_id=theirs, scope="conversation", content="别的会话笔记")
    await remember(
        db_session,
        conversation_id=mine,
        scope="workspace",
        content="本目录约定",
        workspace_path=str(my_root.resolve()),
    )
    await remember(
        db_session,
        conversation_id=mine,
        scope="workspace",
        content="别的目录约定",
        workspace_path=str(other_root.resolve()),
    )

    visible = {
        item.content for item in await load_visible_memories(db_session, conversation_id=mine)
    }

    assert visible == {"全局偏好", "本会话笔记", "本目录约定"}


async def test_list_memories_can_include_forgotten_for_the_panel(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Memory panel")
    record, _ = await remember(
        db_session, conversation_id=conversation_id, scope="global", content="已 retire"
    )
    await forget_memory(db_session, memory_id=record.id)

    active = await list_memories(db_session, conversation_id=conversation_id, workspace_paths=[])
    archived = await list_memories(
        db_session,
        conversation_id=conversation_id,
        workspace_paths=[],
        include_forgotten=True,
    )

    assert active == []
    assert [item.id for item in archived] == [record.id]
    assert memory_payload(archived[0])["forgotten"] is True


async def test_content_length_is_bounded(db_session: AsyncSession) -> None:
    conversation_id = await ensure_conversation(db_session, title="Memory bounds")

    with pytest.raises(MemoryScopeError):
        await remember(db_session, conversation_id=conversation_id, scope="global", content="   ")
    with pytest.raises(MemoryScopeError):
        await remember(
            db_session,
            conversation_id=conversation_id,
            scope="global",
            content="x" * 4001,
        )
