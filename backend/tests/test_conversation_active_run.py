import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.db import DbSession as AsyncSession
from app.runstore.conversations import get_conversation
from app.runstore.runs import create_run, ensure_conversation, finish_run


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
