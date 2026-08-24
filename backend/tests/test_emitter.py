import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import DbSession as AsyncSession
from app.core.db import session_factory
from app.core.run_bus import InMemoryRunBus
from app.runstore.runs import create_run, ensure_conversation, list_events
from app.worker.emitter import RunEventEmitter


async def _run(db_session: AsyncSession):
    conversation_id = await ensure_conversation(db_session)
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="测试流式输出",
        budget_tokens=100,
        budget_calls=1,
        budget_wall_ms=1_000,
    )
    await db_session.commit()
    return run


@pytest.mark.integration
async def test_emitter_flushes_small_delta_when_timer_expires(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    run = await _run(db_session)
    emitter = RunEventEmitter(
        session_factory,
        InMemoryRunBus(),
        run_id=run.id,
        flush_interval_s=0.02,
        flush_chars=10_000,
    )

    await emitter.delta("H")
    for _ in range(30):
        events = await list_events(db_session, run_id=run.id)
        if events:
            break
        await asyncio.sleep(0.01)

    assert [(event.type, event.payload) for event in events] == [("message.delta", {"text": "H"})]


@pytest.mark.integration
async def test_emitter_drains_text_and_reasoning_as_separate_ordered_batches(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    run = await _run(db_session)
    emitter = RunEventEmitter(
        session_factory,
        InMemoryRunBus(),
        run_id=run.id,
        flush_interval_s=60,
        flush_chars=10_000,
    )

    await emitter.reasoning("先")
    await emitter.reasoning("思考")
    await emitter.delta("再")
    await emitter.delta("回答")
    await emitter.drain()

    events = await list_events(db_session, run_id=run.id)
    assert [(event.type, event.payload) for event in events] == [
        ("message.reasoning", {"text": "先思考"}),
        ("message.delta", {"text": "再回答"}),
    ]


@pytest.mark.integration
async def test_structured_event_flushes_pending_text_before_itself(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    run = await _run(db_session)
    emitter = RunEventEmitter(
        session_factory,
        InMemoryRunBus(),
        run_id=run.id,
        flush_interval_s=60,
        flush_chars=10_000,
    )

    await emitter.delta("完整回答")
    await emitter.emit("message.done", {"status": "completed"})

    events = await list_events(db_session, run_id=run.id)
    assert [event.type for event in events] == ["message.delta", "message.done"]
