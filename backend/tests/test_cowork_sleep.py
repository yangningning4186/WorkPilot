"""自唤醒：run 在等时间，而不是在等人。

需要等待的任务此前只有两条烂路：占着 worker 空转，或者结束运行让用户手动再开一轮——
后者会把整个上下文丢掉。这里验证挂起、到点被领走、恢复同一份 checkpoint 这条闭环，
以及"睡眠不烧墙钟预算"这个容易出错的地方。
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.core.db import DbSession as AsyncSession
from app.core.db import session_factory
from app.core.run_bus import InMemoryRunBus
from app.cowork.permissions import create_session_root
from app.cowork.runtime import initialize_cowork_state, load_cowork_checkpoint
from app.cowork.schedules import claim_due_sleeping_runs
from app.cowork.sleep import resolve_wake_at
from app.cowork.tools import build_default_cowork_registry
from app.runstore.runs import append_message, create_run, get_run, list_events, request_cancel
from app.worker.cowork_run import cowork_run
from tests.test_cowork_runner import NativeToolProvider, _final_completion, _tool_completion
from workpilot_ai.gateway import ModelGateway
from workpilot_ai.types import ToolCall

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)


def test_wake_time_rejects_the_ways_a_model_gets_it_wrong() -> None:
    """写错唤醒时间的后果是 run 永远醒不过来，所以每种写法都要当场拒绝。"""

    assert resolve_wake_at(seconds=60, until=None, now=_NOW, max_seconds=3600) == _NOW + timedelta(
        seconds=60
    )
    # 不带时区按 UTC 解释：猜本地时区会让唤醒时刻悄悄偏移几小时。
    naive = "2026-08-21T11:00:00"
    assert resolve_wake_at(seconds=None, until=naive, now=_NOW, max_seconds=7200) == datetime(
        2026, 8, 21, 11, 0, tzinfo=UTC
    )

    with pytest.raises(ValueError, match="只能提供一个"):
        resolve_wake_at(seconds=60, until=naive, now=_NOW, max_seconds=3600)
    with pytest.raises(ValueError, match="只能提供一个"):
        resolve_wake_at(seconds=None, until=None, now=_NOW, max_seconds=3600)
    with pytest.raises(ValueError, match="必须晚于当前时间"):
        resolve_wake_at(seconds=None, until="2026-08-21T09:00:00+00:00", now=_NOW, max_seconds=3600)
    # 超过单次上限时把出路指出来，而不是只说"不行"。
    with pytest.raises(ValueError, match="create_schedule"):
        resolve_wake_at(seconds=7200, until=None, now=_NOW, max_seconds=3600)
    with pytest.raises(ValueError, match="ISO-8601"):
        resolve_wake_at(seconds=None, until="下午三点", now=_NOW, max_seconds=3600)


async def _sleeping_run(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path, seconds: int
):
    from app.runstore.runs import ensure_conversation

    conversation_id = await ensure_conversation(
        db_session, title="Cowork sleep"
    )
    await create_session_root(
        db_session,
        conversation_id=conversation_id,
        requested_path=str(tmp_path),
        access_mode="read_write",
    )
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="等构建跑完再看结果",
        budget_tokens=40_000,
        budget_calls=30,
        budget_wall_ms=120_000,
        workflow_type="cowork",
    )
    await append_message(
        db_session,
        conversation_id=conversation_id,
        role="user",
        content=run.goal,
        run_id=run.id,
    )
    await db_session.commit()
    bus = InMemoryRunBus()
    registry = build_default_cowork_registry()
    await initialize_cowork_state(db_session, run_id=run.id, registry=registry, bus=bus)
    provider = NativeToolProvider(
        [
            _tool_completion(
                ToolCall(
                    id="sleep-1",
                    name="sleep",
                    arguments=json.dumps(
                        {"seconds": seconds, "reason": "等构建结束"}, ensure_ascii=False
                    ),
                )
            ),
            _final_completion("构建已经结束，结果正常。"),
        ]
    )
    context = {
        "settings": get_settings().model_copy(
            update={"cowork_max_steps": 6, "cowork_decision_max_tokens": 2048, "run_heartbeat_s": 60.0}
        ),
        "session_factory": session_factory,
        "bus": bus,
        "cowork_gateway": ModelGateway(provider, embedding_dimensions=1024),
        "cowork_registry": registry,
    }
    await cowork_run(context, str(run.id))
    return run, provider, context


async def test_sleep_parks_the_run_then_resumes_the_same_checkpoint(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path, store_sql
) -> None:
    run, provider, context = await _sleeping_run(db_engine, db_session, tmp_path, seconds=1)

    parked = await get_run(db_session, run.id)
    assert parked is not None and parked.status == "sleeping"
    # 不占 worker：租约必须让出去。
    assert parked.worker_id is None and parked.lease_until is None

    events = await list_events(db_session, run_id=run.id, limit=200)
    sleeping = next(event for event in events if event.type == "run.sleeping")
    assert sleeping.payload["reason"] == "等构建结束"

    checkpoint = await load_cowork_checkpoint(db_session, run_id=run.id)
    assert checkpoint is not None and checkpoint.state["status"] == "sleeping"
    # 工具结果当场写进历史：恢复时缺一条 tool result，provider 会拒绝整个请求。
    last = checkpoint.state["messages"][-1]
    assert last["role"] == "tool" and last["tool_call_id"] == "sleep-1"

    # 还没到点：tick 不该把它捞出来。
    assert await claim_due_sleeping_runs(db_session, now=datetime.now(UTC) - timedelta(hours=1)) == []

    woken = await claim_due_sleeping_runs(db_session, now=datetime.now(UTC) + timedelta(minutes=5))
    await db_session.commit()
    assert woken == [run.id]
    requeued = await get_run(db_session, run.id)
    assert requeued is not None and requeued.status == "queued"

    await cowork_run(context, str(run.id))
    done = await get_run(db_session, run.id)
    assert done is not None and done.status == "done"
    # 恢复的是同一个 run：第二轮看得到第一轮的目标和那条 sleep 结果。
    resumed = provider.tool_histories[-1]
    assert any(message.tool_call_id == "sleep-1" for message in resumed)
    assert any("等构建跑完再看结果" in (message.content or "") for message in resumed)


async def test_sleeping_does_not_burn_the_wall_clock_budget(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path, store_sql
) -> None:
    """墙钟按分段计时，睡眠期间没有开着的分段——否则睡一小时会直接把预算烧穿。"""

    run, _, _ = await _sleeping_run(db_engine, db_session, tmp_path, seconds=3600)
    checkpoint = await load_cowork_checkpoint(db_session, run_id=run.id)
    assert checkpoint is not None

    budget = checkpoint.state["budget"]
    assert budget["used_wall_ms"] < 60_000
    assert budget["used_wall_ms"] < budget["max_wall_ms"]


async def test_a_sleeping_run_can_still_be_cancelled(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path, store_sql
) -> None:
    """睡着的 run 没有 worker 在跑，取消应当就地结案而不是等它醒。"""

    run, _, _ = await _sleeping_run(db_engine, db_session, tmp_path, seconds=3600)

    assert await request_cancel(db_session, run_id=run.id) is not None
    await db_session.commit()

    cancelled = await get_run(db_session, run.id)
    assert cancelled is not None and cancelled.status == "cancelled"
    # 已取消的 run 不该再被唤醒。
    assert await claim_due_sleeping_runs(db_session, now=datetime.now(UTC) + timedelta(days=1)) == []


async def test_wake_claim_is_atomic_so_a_run_is_never_queued_twice(
    db_engine: AsyncEngine, db_session: AsyncSession, tmp_path: Path, store_sql
) -> None:
    """tick 可能与另一个 worker 撞车；分两步做会让同一份 checkpoint 被恢复两次。"""

    run, _, _ = await _sleeping_run(db_engine, db_session, tmp_path, seconds=1)
    later = datetime.now(UTC) + timedelta(minutes=5)

    first = await claim_due_sleeping_runs(db_session, now=later)
    await db_session.commit()
    second = await claim_due_sleeping_runs(db_session, now=later)
    await db_session.commit()

    assert first == [run.id]
    assert second == []
    remaining = store_sql(
        "SELECT wake_at FROM agent_runs WHERE id = ?", (str(run.id),)
    )[0]["wake_at"]
    assert remaining is None
