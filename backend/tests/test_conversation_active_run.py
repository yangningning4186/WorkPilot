import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import DbSession as AsyncSession
from app.cowork.runtime import _load_previous_branch_checkpoint
from app.cowork_store.routing import cowork_store
from app.runstore.conversations import (
    ConversationBusyError,
    fork_conversation,
    get_conversation,
    list_conversation_entries,
    list_conversation_messages,
    navigate_conversation_lane,
)
from app.runstore.runs import append_message, create_run, ensure_conversation, finish_run


@pytest.mark.integration
async def test_conversation_exposes_active_run_until_it_reaches_terminal(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="后台任务")
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="切走后继续执行",
        budget_tokens=100,
        budget_calls=1,
        budget_wall_ms=1_000,
    )
    await db_session.commit()

    active = await get_conversation(db_session, conversation_id=conversation_id)
    assert active is not None and active.active_run_id == run.id

    await finish_run(db_session, run_id=run.id, status="done")
    await db_session.commit()
    finished = await get_conversation(db_session, conversation_id=conversation_id)
    assert finished is not None and finished.active_run_id is None


async def test_conversation_can_fork_from_any_completed_message(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="原会话")
    source_ids = [
        await append_message(
            db_session,
            conversation_id=conversation_id,
            role=role,
            content=content,
        )
        for role, content in (
            ("user", "第一问"),
            ("assistant", "第一答"),
            ("user", "第二问"),
            ("assistant", "第二答"),
        )
    ]

    fork_id = await fork_conversation(
        db_session,
        conversation_id=conversation_id,
        message_id=source_ids[1],
        position="after",
        title="从第一轮重试",
    )

    source = await list_conversation_messages(db_session, conversation_id=conversation_id)
    forked = await list_conversation_messages(db_session, conversation_id=fork_id)
    entries = await list_conversation_entries(db_session, conversation_id=fork_id)
    assert source is not None and [item.content for item in source] == [
        "第一问",
        "第一答",
        "第二问",
        "第二答",
    ]
    assert forked is not None and [item.content for item in forked] == ["第一问", "第一答"]
    assert [item.id for item in forked] != source_ids[:2]
    assert entries is not None and [item.kind for item in entries] == [
        "message",
        "message",
        "branch_summary",
    ]
    assert entries[-1].payload["abandoned_messages"] == 2
    assert entries[-1].payload["source_message_id"] == str(source_ids[1])


async def test_conversation_refuses_to_fork_while_a_run_is_active(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="运行中")
    message_id = await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content="尚未完成",
    )
    await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="尚未完成",
        budget_tokens=100,
        budget_calls=1,
        budget_wall_ms=1_000,
    )

    with pytest.raises(ConversationBusyError):
        await fork_conversation(
            db_session,
            conversation_id=conversation_id,
            message_id=message_id,
        )


async def test_lane_navigation_changes_visible_messages_and_preserves_old_branch(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="树内导航")
    message_ids = [
        await append_message(
            db_session,
            conversation_id=conversation_id,
            role=role,
            content=content,
        )
        for role, content in (
            ("user", "第一问"),
            ("assistant", "第一答"),
            ("user", "旧分支第二问"),
            ("assistant", "旧分支第二答"),
        )
    ]

    navigation = await navigate_conversation_lane(
        db_session,
        conversation_id=conversation_id,
        target_entry_id=f"message:{message_ids[1]}",
        summarize=True,
    )
    assert navigation.abandoned_lane is not None
    assert navigation.branch_summary_entry_id is not None
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content="新分支第二问",
    )

    active = await list_conversation_messages(db_session, conversation_id=conversation_id)
    abandoned = await list_conversation_messages(
        db_session,
        conversation_id=conversation_id,
        lane=navigation.abandoned_lane,
    )
    abandoned_entries = await list_conversation_entries(
        db_session,
        conversation_id=conversation_id,
        lane=navigation.abandoned_lane,
    )
    metadata = await get_conversation(db_session, conversation_id=conversation_id)

    assert active is not None and [item.content for item in active] == [
        "第一问",
        "第一答",
        "新分支第二问",
    ]
    assert abandoned is not None and [item.content for item in abandoned] == [
        "第一问",
        "第一答",
        "旧分支第二问",
        "旧分支第二答",
    ]
    assert abandoned_entries is not None
    summary_entry = abandoned_entries[-1]
    assert summary_entry.kind == "branch_summary"
    assert "user: 旧分支第二问" in summary_entry.payload["summary"]
    assert "assistant: 旧分支第二答" in summary_entry.payload["summary"]
    assert summary_entry.payload["abandoned_message_ids"] == [
        str(message_ids[2]),
        str(message_ids[3]),
    ]
    assert metadata is not None
    assert metadata.message_count == 3
    assert metadata.latest_message == "新分支第二问"


async def test_lane_navigation_refuses_to_race_an_active_run(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="导航竞态")
    message_id = await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content="第一问",
    )
    await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="运行中",
        budget_tokens=100,
        budget_calls=1,
        budget_wall_ms=1_000,
    )

    with pytest.raises(ConversationBusyError, match="不能移动会话分支"):
        await navigate_conversation_lane(
            db_session,
            conversation_id=conversation_id,
            target_entry_id=f"message:{message_id}",
        )


async def test_new_run_inherits_checkpoint_from_selected_branch(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="分支 checkpoint")
    store = cowork_store()

    run_one = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="第一轮",
        budget_tokens=100,
        budget_calls=1,
        budget_wall_ms=1_000,
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content="第一问",
        run_id=run_one.id,
    )
    await store.commit_checkpoint(
        run_id=run_one.id,
        state={"marker": "one", "status": "done", "messages": []},
        parent_id=None,
        checkpoint_id="run-one-terminal",
        used_tokens=0,
        used_calls=0,
        events=[],
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="assistant",
        content="第一答",
        run_id=run_one.id,
    )
    await finish_run(db_session, run_id=run_one.id, status="done")

    run_two = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="第二轮",
        budget_tokens=100,
        budget_calls=1,
        budget_wall_ms=1_000,
    )
    user_two = await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content="旧第二问",
        run_id=run_two.id,
    )
    await store.commit_checkpoint(
        run_id=run_two.id,
        state={"marker": "two", "status": "done", "messages": []},
        parent_id=None,
        checkpoint_id="run-two-terminal",
        used_tokens=0,
        used_calls=0,
        events=[],
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="assistant",
        content="旧第二答",
        run_id=run_two.id,
    )
    await finish_run(db_session, run_id=run_two.id, status="done")

    await navigate_conversation_lane(
        db_session,
        conversation_id=conversation_id,
        target_entry_id=f"message:{user_two}",
        position="before",
    )
    current = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="新第二轮",
        budget_tokens=100,
        budget_calls=1,
        budget_wall_ms=1_000,
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content="新第二问",
        run_id=current.id,
    )

    previous = await _load_previous_branch_checkpoint(run_id=current.id)

    assert previous is not None
    assert previous.run_id == run_one.id
    assert previous.state["marker"] == "one"
