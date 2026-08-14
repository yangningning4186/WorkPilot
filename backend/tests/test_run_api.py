from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.dependencies import get_run_bus, get_run_queue_dependency, get_session_factory
from app.core.db import get_db_session
from app.core.run_bus import InMemoryRunBus
from app.main import create_app
from app.services.runs import (
    append_events,
    claim_run,
    create_run,
    ensure_conversation,
    finish_run,
    get_run,
)

pytestmark = pytest.mark.integration


class RecordingQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[UUID, int]] = []

    async def enqueue_answer_run(self, run_id: UUID, *, top_k: int) -> None:
        self.enqueued.append((run_id, top_k))


class BrokenQueue:
    async def enqueue_answer_run(self, run_id: UUID, *, top_k: int) -> None:
        raise ConnectionError("redis 不可达")


def _client(
    db_session: AsyncSession, queue: object, db_engine: AsyncEngine | None = None
) -> tuple[httpx.AsyncClient, InMemoryRunBus]:
    async def override_session():
        yield db_session

    bus = InMemoryRunBus()
    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_run_queue_dependency] = lambda: queue
    app.dependency_overrides[get_run_bus] = lambda: bus
    if db_engine is not None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        app.dependency_overrides[get_session_factory] = lambda: factory
    return (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"),
        bus,
    )


async def test_create_run_returns_immediately_and_enqueues(db_session: AsyncSession) -> None:
    queue = RecordingQueue()
    client, _ = _client(db_session, queue)
    async with client:
        response = await client.post(
            "/api/v1/runs", json={"query": "稠密检索如何召回内容?", "top_k": 3}
        )

    assert response.status_code == 202
    payload = response.json()
    run_id = UUID(payload["run_id"])
    assert payload["status"] == "queued"
    assert queue.enqueued == [(run_id, 3)]

    run = await get_run(db_session, run_id)
    assert run is not None
    assert run.goal == "稠密检索如何召回内容?"

    # 用户消息在创建时就落库, 刷新页面能看到自己问了什么。
    role, content = (
        await db_session.execute(
            text("SELECT role, content FROM messages WHERE conversation_id = :id"),
            {"id": run.conversation_id},
        )
    ).one()
    assert (role, content) == ("user", "稠密检索如何召回内容?")


async def test_enqueue_failure_finalizes_run_instead_of_leaving_it_queued_forever(
    db_session: AsyncSession,
) -> None:
    """入队失败的 run 没有租约, watchdog 也捞不到, 必须当场落终态。"""

    client, _ = _client(db_session, BrokenQueue())
    async with client:
        response = await client.post("/api/v1/runs", json={"query": "问题"})
    assert response.status_code == 503

    run_id = (
        await db_session.execute(text("SELECT id FROM agent_runs ORDER BY created_at DESC LIMIT 1"))
    ).scalar_one()
    run = await get_run(db_session, UUID(str(run_id)))
    assert run is not None
    assert run.status == "failed"


async def test_unknown_conversation_is_rejected(db_session: AsyncSession) -> None:
    client, _ = _client(db_session, RecordingQueue())
    async with client:
        response = await client.post(
            "/api/v1/runs", json={"query": "问题", "conversation_id": str(uuid4())}
        )
    assert response.status_code == 404


async def test_events_endpoint_replays_history_as_sse(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    conversation_id = await ensure_conversation(db_session)
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="问题",
        budget_tokens=10,
        budget_calls=1,
        budget_wall_ms=1000,
    )
    await append_events(
        db_session,
        run_id=run.id,
        events=[("message.delta", {"text": "一"}), ("message.done", {"message_id": "m"})],
    )
    await finish_run(db_session, run_id=run.id, status="done")
    await db_session.commit()

    client, _ = _client(db_session, RecordingQueue(), db_engine)
    async with client:
        response = await client.get(f"/api/v1/runs/{run.id}/events")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        body = response.text

        # Last-Event-ID 优先于 after_seq, 这是浏览器自动重连的续传机制。
        resumed = await client.get(
            f"/api/v1/runs/{run.id}/events",
            headers={"Last-Event-ID": f"{run.id}:1"},
        )

    assert f"id: {run.id}:1" in body
    assert "event: message.delta" in body
    assert f"id: {run.id}:1" not in resumed.text
    assert f"id: {run.id}:2" in resumed.text


async def test_events_endpoint_404s_for_unknown_run(db_session: AsyncSession) -> None:
    client, _ = _client(db_session, RecordingQueue())
    async with client:
        response = await client.get(f"/api/v1/runs/{uuid4()}/events")
    assert response.status_code == 404


async def test_cancel_marks_request_and_wakes_subscribers(db_session: AsyncSession) -> None:
    conversation_id = await ensure_conversation(db_session)
    run = await create_run(
        db_session,
        conversation_id=conversation_id,
        goal="问题",
        budget_tokens=10,
        budget_calls=1,
        budget_wall_ms=1000,
    )
    await claim_run(db_session, run_id=run.id, worker_id="worker-a", lease_s=60)
    await db_session.commit()

    client, _ = _client(db_session, RecordingQueue())
    async with client:
        response = await client.post(f"/api/v1/runs/{run.id}/cancel")

    assert response.status_code == 200
    payload = response.json()
    assert payload["cancel_requested"] is True
    # 执行中的 run 由 worker 自己收尾, 接口不直接改成终态。
    assert payload["status"] == "executing"
