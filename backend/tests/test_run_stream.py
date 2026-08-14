import asyncio
import json

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.run_bus import InMemoryRunBus
from app.services.run_stream import parse_last_event_id, stream_run_events
from app.services.runs import append_events, create_run, ensure_conversation, finish_run


def test_last_event_id_parsing_tolerates_garbage() -> None:
    assert parse_last_event_id("0198f0f0-0000-7000-8000-000000000000:42") == 42
    assert parse_last_event_id("42") is None
    assert parse_last_event_id("run:abc") is None
    assert parse_last_event_id("run:-1") is None
    assert parse_last_event_id(None) is None


@pytest.mark.integration
async def test_stream_replays_history_then_stops_at_terminal(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    conversation_id = await ensure_conversation(db_session)
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="问题",
        budget_tokens=100,
        budget_calls=1,
        budget_wall_ms=1000,
    )
    await append_events(
        db_session,
        run_id=run.id,
        events=[
            ("message.start", {"message_id": "m1"}),
            ("message.delta", {"text": "答案"}),
            ("message.done", {"message_id": "m1"}),
        ],
    )
    await finish_run(db_session, run_id=run.id, status="done")
    await db_session.commit()

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    frames = [
        frame
        async for frame in stream_run_events(factory, InMemoryRunBus(), run_id=run.id, after_seq=0)
    ]

    assert len(frames) == 3
    assert frames[0].startswith(f"id: {run.id}:1\nevent: message.start\ndata: ")
    envelope = json.loads(frames[1].split("data: ", 1)[1])
    assert envelope == {
        "id": f"{run.id}:2",
        "run_id": str(run.id),
        "seq": "2",
        "type": "message.delta",
        "data": {"text": "答案"},
    }


@pytest.mark.integration
async def test_stream_resumes_from_cursor_without_replaying_seen_events(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """B2: 断线重连带 Last-Event-ID, 服务端从该位置续发。"""

    conversation_id = await ensure_conversation(db_session)
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="问题",
        budget_tokens=100,
        budget_calls=1,
        budget_wall_ms=1000,
    )
    await append_events(
        db_session,
        run_id=run.id,
        events=[("message.delta", {"text": "一"}), ("message.delta", {"text": "二"})],
    )
    await finish_run(db_session, run_id=run.id, status="done")
    await db_session.commit()

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    frames = [
        frame
        async for frame in stream_run_events(factory, InMemoryRunBus(), run_id=run.id, after_seq=1)
    ]
    assert len(frames) == 1
    assert '"seq": "2"' in frames[0]


@pytest.mark.integration
async def test_stream_picks_up_events_written_after_subscription(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """先订阅再查库: 否则两步之间产生的事件既不在历史里也收不到通知。"""

    conversation_id = await ensure_conversation(db_session)
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="问题",
        budget_tokens=100,
        budget_calls=1,
        budget_wall_ms=1000,
    )
    await db_session.commit()

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    bus = InMemoryRunBus()
    frames: list[str] = []

    async def consume() -> None:
        async for frame in stream_run_events(
            factory, bus, run_id=run.id, after_seq=0, heartbeat_s=0.05
        ):
            frames.append(frame)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.1)

    async with factory() as session:
        await append_events(session, run_id=run.id, events=[("message.delta", {"text": "迟到"})])
        await session.commit()
    await bus.publish(run.id)
    await asyncio.sleep(0.1)

    async with factory() as session:
        await append_events(session, run_id=run.id, events=[("message.done", {})])
        await finish_run(session, run_id=run.id, status="done")
        await session.commit()
    await bus.publish(run.id)

    await asyncio.wait_for(consumer, timeout=3)

    events = [frame for frame in frames if frame.startswith("id: ")]
    assert [frame.split("event: ")[1].split("\n")[0] for frame in events] == [
        "message.delta",
        "message.done",
    ]
    # 没有新事件时靠注释帧保活, 不会被中间设备判为空闲连接。
    assert any(frame.startswith(": keepalive") for frame in frames)
