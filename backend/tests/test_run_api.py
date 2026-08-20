from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent_core.budget import BudgetMeter
from app.api.dependencies import (
    get_request_identity,
    get_run_bus,
    get_run_queue_dependency,
    get_session_factory,
    require_admin_session,
    require_owner_identity,
)
from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.core.run_bus import InMemoryRunBus
from app.main import create_app
from app.platform.demo_sessions import hash_session_token
from app.platform.request_identity import RequestIdentity
from app.rag.review.graph import run_readonly_review
from app.rag.review.write_note import review_resume_token
from app.runstore.runs import append_events, claim_run, finish_run, get_run
from tests.fakes import review_budget
from tests.test_write_note import ReadyReviewTools

pytestmark = pytest.mark.integration


class RecordingQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[UUID, int]] = []
        self.enqueued_reviews: list[UUID] = []
        self.enqueued_cowork: list[UUID] = []

    async def enqueue_answer_run(self, run_id: UUID, *, top_k: int) -> None:
        self.enqueued.append((run_id, top_k))

    async def enqueue_review_run(self, run_id: UUID, *, attempt: int = 0) -> None:
        self.enqueued_reviews.append(run_id)

    async def enqueue_cowork_run(self, run_id: UUID, *, attempt: int = 0) -> None:
        self.enqueued_cowork.append(run_id)


class BrokenQueue:
    async def enqueue_answer_run(self, run_id: UUID, *, top_k: int) -> None:
        raise ConnectionError("redis 不可达")

    async def enqueue_review_run(self, run_id: UUID, *, attempt: int = 0) -> None:
        raise ConnectionError("redis 不可达")

    async def enqueue_cowork_run(self, run_id: UUID, *, attempt: int = 0) -> None:
        raise ConnectionError("redis 不可达")


def _client(
    db_session: AsyncSession,
    queue: object,
    db_engine: AsyncEngine | None = None,
    *,
    settings: Settings | None = None,
    admin: bool = False,
) -> tuple[httpx.AsyncClient, InMemoryRunBus]:
    async def override_session():
        yield db_session

    bus = InMemoryRunBus()
    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_run_queue_dependency] = lambda: queue
    app.dependency_overrides[get_run_bus] = lambda: bus
    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: settings
    if admin:
        app.dependency_overrides[require_admin_session] = lambda: None
        owner_identity = RequestIdentity(scope="local_owner")
        app.dependency_overrides[get_request_identity] = lambda: owner_identity
        app.dependency_overrides[require_owner_identity] = lambda: owner_identity
    if db_engine is not None:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        app.dependency_overrides[get_session_factory] = lambda: factory
    return (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"),
        bus,
    )


async def test_review_run_api_enqueues_pauses_and_resumes_once(
    db_session: AsyncSession, tmp_path
) -> None:
    queue = RecordingQueue()
    client, _ = _client(
        db_session,
        queue,
        settings=Settings(app_env="test", agent_output_path=tmp_path),
        admin=True,
    )
    document_ids = [uuid4(), uuid4()]
    async with client:
        created_response = await client.post(
            "/api/v1/runs/reviews",
            json={
                "goal": "比较记忆方法",
                "document_ids": [str(item) for item in document_ids],
                "output_path": "reviews/memory.md",
            },
        )
        assert created_response.status_code == 202
        created = created_response.json()
        run_id = UUID(created["run_id"])
        assert created["workflow_type"] == "literature_review"
        assert queue.enqueued_reviews == [run_id]

        state = await run_readonly_review(
            db_session,
            run_id=run_id,
            tools=ReadyReviewTools(),
            meter=BudgetMeter(review_budget()),
        )
        assert state["status"] == "waiting_human"
        response = await client.post(
            f"/api/v1/runs/{run_id}/resume",
            json={"resume_token": review_resume_token(run_id), "approved": True},
        )
        repeated = await client.post(
            f"/api/v1/runs/{run_id}/resume",
            json={"resume_token": review_resume_token(run_id), "approved": True},
        )

    assert response.status_code == repeated.status_code == 200
    assert response.json()["status"] == "done"
    assert (tmp_path / "reviews/memory.md").is_file()


async def _create_http_run(client: httpx.AsyncClient, query: str = "问题") -> dict[str, str]:
    response = await client.post("/api/v1/runs", json={"query": query})
    assert response.status_code == 202
    return response.json()


async def test_conversations_are_explicitly_created_listed_and_identity_isolated(
    db_session: AsyncSession,
) -> None:
    owner, _ = _client(db_session, RecordingQueue(), admin=True)
    async with owner:
        created = await owner.post("/api/v1/conversations", json={"title": "RAG 方案"})
        assert created.status_code == 201
        conversation_id = created.json()["id"]
        run = await owner.post(
            "/api/v1/runs",
            json={"query": "先解释召回", "conversation_id": conversation_id},
        )
        assert run.status_code == 202
        listed = await owner.get("/api/v1/conversations")
        messages = await owner.get(f"/api/v1/conversations/{conversation_id}/messages")

    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == conversation_id
    assert listed.json()["items"][0]["message_count"] == 1
    assert messages.status_code == 200
    assert [item["content"] for item in messages.json()["items"]] == ["先解释召回"]

    anonymous, _ = _client(db_session, RecordingQueue())
    async with anonymous:
        foreign = await anonymous.get(f"/api/v1/conversations/{conversation_id}/messages")
    assert foreign.status_code == 404


async def test_delete_conversation_is_isolated_blocks_active_run_and_cascades(
    db_session: AsyncSession,
) -> None:
    owner, _ = _client(db_session, RecordingQueue(), admin=True)
    async with owner:
        created = await owner.post("/api/v1/conversations", json={"title": "待删除会话"})
        conversation_id = created.json()["id"]
        run_response = await owner.post(
            "/api/v1/runs",
            json={"query": "正在回答的问题", "conversation_id": conversation_id},
        )
        run_id = run_response.json()["run_id"]
        await db_session.execute(
            text(
                """
                UPDATE agent_runs
                SET status = 'executing', worker_id = 'active-delete-test',
                    lease_until = now() + interval '5 minutes'
                WHERE id = :run_id
                """
            ),
            {"run_id": UUID(run_id)},
        )
        await db_session.commit()

        busy = await owner.delete(f"/api/v1/conversations/{conversation_id}")
        assert busy.status_code == 409

        foreign, _ = _client(db_session, RecordingQueue())
        async with foreign:
            hidden = await foreign.delete(f"/api/v1/conversations/{conversation_id}")
        assert hidden.status_code == 404

        await db_session.execute(
            text(
                """
                UPDATE agent_runs
                SET status = 'waiting_human', worker_id = NULL, lease_until = NULL
                WHERE id = :run_id
                """
            ),
            {"run_id": UUID(run_id)},
        )
        await db_session.commit()

        deleted = await owner.delete(f"/api/v1/conversations/{conversation_id}")
        missing = await owner.get(f"/api/v1/conversations/{conversation_id}/messages")

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert missing.status_code == 404
    counts = (
        await db_session.execute(
            text(
                """
                SELECT
                    (SELECT COUNT(*) FROM conversations WHERE id = :conversation_id),
                    (SELECT COUNT(*) FROM agent_runs WHERE id = :run_id),
                    (SELECT COUNT(*) FROM messages WHERE conversation_id = :conversation_id)
                """
            ),
            {"conversation_id": UUID(conversation_id), "run_id": UUID(run_id)},
        )
    ).one()
    assert counts == (0, 0, 0)


async def test_conversation_can_be_archived_filtered_and_restored(
    db_session: AsyncSession,
) -> None:
    owner, _ = _client(db_session, RecordingQueue(), admin=True)
    async with owner:
        created = await owner.post("/api/v1/conversations", json={"title": "季度复盘"})
        conversation_id = created.json()["id"]
        archived = await owner.put(
            f"/api/v1/conversations/{conversation_id}/archive",
            json={"archived": True},
        )
        active_list = await owner.get("/api/v1/conversations")
        archived_list = await owner.get("/api/v1/conversations?archived=true")
        rejected_run = await owner.post(
            "/api/v1/runs",
            json={"query": "继续复盘", "conversation_id": conversation_id},
        )
        restored = await owner.put(
            f"/api/v1/conversations/{conversation_id}/archive",
            json={"archived": False},
        )
        restored_list = await owner.get("/api/v1/conversations")

    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    assert all(item["id"] != conversation_id for item in active_list.json()["items"])
    assert [item["id"] for item in archived_list.json()["items"]] == [conversation_id]
    assert rejected_run.status_code == 404
    assert restored.status_code == 200
    assert restored.json()["archived_at"] is None
    assert any(item["id"] == conversation_id for item in restored_list.json()["items"])


async def test_create_run_sets_http_only_session_and_enqueues(
    db_session: AsyncSession,
) -> None:
    queue = RecordingQueue()
    client, _ = _client(db_session, queue)
    async with client:
        response = await client.post(
            "/api/v1/runs", json={"query": "稠密检索如何召回内容?", "top_k": 3}
        )

    assert response.status_code == 202
    set_cookie = response.headers["set-cookie"].lower()
    assert "workpilot_session=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "max-age=1800" in set_cookie

    payload = response.json()
    run_id = UUID(payload["run_id"])
    assert payload["status"] == "queued"
    assert queue.enqueued == [(run_id, 3)]

    run = await get_run(db_session, run_id)
    assert run is not None
    assert run.goal == "稠密检索如何召回内容?"
    assert run.retrieval_top_k == 3
    scope, demo_session_id = (
        await db_session.execute(
            text("SELECT scope, demo_session_id FROM conversations WHERE id = :id"),
            {"id": run.conversation_id},
        )
    ).one()
    assert scope == "demo"
    assert demo_session_id is not None

    raw_cookie = client.cookies["workpilot_session"]
    token_hash = (
        await db_session.execute(
            text("SELECT token_hash FROM demo_sessions WHERE id = :id"),
            {"id": demo_session_id},
        )
    ).scalar_one()
    assert token_hash == hash_session_token(raw_cookie)
    assert raw_cookie not in token_hash

    # 用户消息在创建时就落库, 刷新页面能看到自己问了什么。
    role, content = (
        await db_session.execute(
            text("SELECT role, content FROM messages WHERE conversation_id = :id"),
            {"id": run.conversation_id},
        )
    ).one()
    assert (role, content) == ("user", "稠密检索如何召回内容?")


async def test_logged_in_run_uses_owner_scope_without_demo_quota(
    db_session: AsyncSession,
) -> None:
    queue = RecordingQueue()
    client, _ = _client(
        db_session,
        queue,
        settings=Settings(app_env="test", demo_session_question_limit=1),
        admin=True,
    )
    async with client:
        response = await client.post("/api/v1/runs", json={"query": "请记住我的偏好"})
        second = await client.post("/api/v1/runs", json={"query": "第二个 owner 问题"})

    assert response.status_code == 202
    assert second.status_code == 202
    conversation_id = UUID(response.json()["conversation_id"])
    scope, demo_session_id = (
        await db_session.execute(
            text("SELECT scope, demo_session_id FROM conversations WHERE id = :id"),
            {"id": conversation_id},
        )
    ).one()
    assert scope == "local_owner"
    assert demo_session_id is None
    assert "workpilot_session=" not in response.headers.get("set-cookie", "").lower()


async def test_production_session_cookie_is_secure(db_session: AsyncSession) -> None:
    client, _ = _client(
        db_session,
        RecordingQueue(),
        settings=Settings(
            app_env="production",
            session_cookie_secure=None,
            ip_rate_limit_enabled=False,
        ),
    )
    async with client:
        response = await client.post("/api/v1/runs", json={"query": "问题"})

    assert response.status_code == 202
    assert "secure" in response.headers["set-cookie"].lower()


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


async def test_foreign_conversation_cannot_be_reused(db_session: AsyncSession) -> None:
    owner, _ = _client(db_session, RecordingQueue())
    intruder, _ = _client(db_session, RecordingQueue())
    async with owner, intruder:
        created = await _create_http_run(owner)
        response = await intruder.post(
            "/api/v1/runs",
            json={"query": "越权", "conversation_id": created["conversation_id"]},
        )

    assert response.status_code == 404


async def test_events_endpoint_replays_history_as_sse(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    client, _ = _client(db_session, RecordingQueue(), db_engine)
    async with client:
        created = await _create_http_run(client)
        run_id = UUID(created["run_id"])
        await append_events(
            db_session,
            run_id=run_id,
            events=[("message.delta", {"text": "一"}), ("message.done", {"message_id": "m"})],
        )
        await finish_run(db_session, run_id=run_id, status="done")
        await db_session.commit()

        response = await client.get(f"/api/v1/runs/{run_id}/events")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        body = response.text

        # Last-Event-ID 优先于 after_seq, 这是浏览器自动重连时的续传游标。
        resumed = await client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": f"{run_id}:1"},
        )
        event_log = await client.get(f"/api/v1/runs/{run_id}/event-log?after_seq=1")

    assert f"id: {run_id}:1" in body
    assert "event: message.delta" in body
    assert f"id: {run_id}:1" not in resumed.text
    assert f"id: {run_id}:2" in resumed.text
    assert event_log.status_code == 200
    assert event_log.json() == {
        "items": [
            {
                "id": f"{run_id}:2",
                "run_id": str(run_id),
                "seq": "2",
                "type": "message.done",
                "data": {"message_id": "m"},
            }
        ]
    }


async def test_events_endpoint_404s_for_unknown_run(db_session: AsyncSession) -> None:
    client, _ = _client(db_session, RecordingQueue())
    async with client:
        response = await client.get(f"/api/v1/runs/{uuid4()}/events")
    assert response.status_code == 404


async def test_other_session_cannot_read_stream_or_cancel_run(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    owner, _ = _client(db_session, RecordingQueue(), db_engine)
    intruder, _ = _client(db_session, RecordingQueue(), db_engine)
    async with owner, intruder:
        created = await _create_http_run(owner)
        run_id = UUID(created["run_id"])

        assert (await owner.get(f"/api/v1/runs/{run_id}")).status_code == 200
        assert (await intruder.get(f"/api/v1/runs/{run_id}")).status_code == 404
        assert (await intruder.get(f"/api/v1/runs/{run_id}/events")).status_code == 404
        assert (await intruder.get(f"/api/v1/runs/{run_id}/event-log")).status_code == 404
        assert (await intruder.post(f"/api/v1/runs/{run_id}/cancel")).status_code == 404

        still_queued = await get_run(db_session, run_id)
        assert still_queued is not None
        assert still_queued.status == "queued"


async def test_cancel_marks_request_and_wakes_subscribers(db_session: AsyncSession) -> None:
    client, _ = _client(db_session, RecordingQueue())
    async with client:
        created = await _create_http_run(client)
        run_id = UUID(created["run_id"])
        await claim_run(db_session, run_id=run_id, worker_id="worker-a", lease_s=60)
        await db_session.commit()

        response = await client.post(f"/api/v1/runs/{run_id}/cancel")

    assert response.status_code == 200
    payload = response.json()
    assert payload["cancel_requested"] is True
    # 执行中的 run 由 worker 自己收尾, 接口不直接改成终态。
    assert payload["status"] == "executing"


async def test_cancel_queued_run_appends_terminal_events(db_session: AsyncSession) -> None:
    client, _ = _client(db_session, RecordingQueue())
    async with client:
        created = await _create_http_run(client)
        run_id = UUID(created["run_id"])

        response = await client.post(f"/api/v1/runs/{run_id}/cancel")
        repeated = await client.post(f"/api/v1/runs/{run_id}/cancel")
        event_log = await client.get(f"/api/v1/runs/{run_id}/event-log")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert repeated.status_code == 200
    assert repeated.json()["next_seq"] == response.json()["next_seq"]
    terminal = event_log.json()["items"][-2:]
    assert [event["type"] for event in terminal] == ["error", "run.done"]
    assert terminal[0]["data"]["code"] == "cancelled"
    assert terminal[1]["data"] == {
        "workflow_type": "answer",
        "status": "cancelled",
    }


async def test_forged_or_expired_cookie_rotates_to_new_session(
    db_session: AsyncSession,
) -> None:
    client, _ = _client(db_session, RecordingQueue())
    client.cookies.set("workpilot_session", "forged-token", domain="test.local", path="/")
    async with client:
        first = await _create_http_run(client, "第一次")
        first_cookie = client.cookies["workpilot_session"]
        assert first_cookie != "forged-token"
        first_session_id = (
            await db_session.execute(
                text("SELECT demo_session_id FROM conversations WHERE id = :id"),
                {"id": UUID(first["conversation_id"])},
            )
        ).scalar_one()
        await db_session.execute(
            text(
                """
                UPDATE demo_sessions
                SET created_at = now() - interval '2 days',
                    expires_at = now() - interval '1 day'
                WHERE id = :id
                """
            ),
            {"id": first_session_id},
        )
        await db_session.commit()

        second = await _create_http_run(client, "第二次")
        second_session_id = (
            await db_session.execute(
                text("SELECT demo_session_id FROM conversations WHERE id = :id"),
                {"id": UUID(second["conversation_id"])},
            )
        ).scalar_one()

    assert client.cookies["workpilot_session"] != first_cookie
    assert second_session_id != first_session_id


async def test_demo_session_question_quota_is_atomic(db_session: AsyncSession) -> None:
    queue = RecordingQueue()
    client, _ = _client(
        db_session,
        queue,
        settings=Settings(app_env="test", demo_session_question_limit=2),
    )
    async with client:
        assert (await client.post("/api/v1/runs", json={"query": "一"})).status_code == 202
        assert (await client.post("/api/v1/runs", json={"query": "二"})).status_code == 202
        exhausted = await client.post("/api/v1/runs", json={"query": "三"})

    assert exhausted.status_code == 429
    assert exhausted.json()["detail"] == "本 session 的提问额度已用尽"
    assert len(queue.enqueued) == 2
    assert (
        await db_session.execute(text("SELECT question_count FROM demo_sessions"))
    ).scalar_one() == 2
