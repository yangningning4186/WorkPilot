from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_core.idempotency import InvocationInFlightError
from app.core.config import Settings
from app.core.queue import InProcessRunQueue
from app.cowork.permissions import (
    DEFAULT_WORKSPACE_LABEL,
    CoworkPermissionError,
    ensure_default_session_root,
)
from app.cowork.runtime import initialize_cowork_state
from app.cowork.tools import build_default_cowork_registry
from app.cowork_store.factory import (
    close_local_cowork_stores,
    initialize_local_cowork_stores,
)
from app.cowork_store.jsonl import JsonlConversationStore, JsonlMessage
from app.cowork_store.sqlite import SqliteCoworkStore
from app.runstore.conversations import ConversationBusyError, get_conversation
from app.runstore.runs import (
    append_message,
    create_run,
    ensure_conversation,
    finish_run,
    reap_expired_runs,
)


async def test_in_process_queue_deduplicates_pending_wakeups() -> None:
    queue = InProcessRunQueue()
    run_id = uuid4()

    await queue.enqueue_cowork_run(run_id)
    await queue.enqueue_cowork_run(run_id, attempt=2)
    task = await queue.get()

    assert task.object_id == run_id
    assert task.name == "cowork_run"
    queue.task_done(task)
    await queue.enqueue_cowork_run(run_id, attempt=2)
    retried = await queue.get()
    assert retried.attempt == 2
    queue.task_done(retried)


async def test_sqlite_store_persists_run_events_checkpoint_and_invocation_lease(
    tmp_path: Path,
) -> None:
    store = SqliteCoworkStore(tmp_path / "state" / "cowork.db")
    await store.initialize()
    conversation_id = await store.create_conversation(title="本地任务")
    run = await store.create_run(
        conversation_id=conversation_id,
        goal="检查项目",
        budget_tokens=1000,
        budget_calls=10,
        budget_wall_ms=30_000,
    )

    assert [item.id for item in await store.list_queued_runs()] == [run.id]
    claimed = await store.claim_run(run_id=run.id, worker_id="worker-a", lease_s=30)
    assert claimed is not None
    assert claimed.status == "executing"
    assert await store.claim_run(run_id=run.id, worker_id="worker-b", lease_s=30) is None
    assert await store.renew_run_lease(run_id=run.id, worker_id="worker-a", lease_s=30)

    events = await store.append_events(
        run_id=run.id,
        events=[("tool.start", {"tool": "read_text_file"}), ("tool.result", {"ok": True})],
    )
    assert [event.seq for event in events] == [1, 2]
    assert [event.type for event in await store.list_events(run_id=run.id)] == [
        "tool.start",
        "tool.result",
    ]

    checkpoint = await store.save_checkpoint(
        run_id=run.id,
        state={"status": "executing", "messages": []},
        parent_id=None,
    )
    assert await store.load_latest_checkpoint(run_id=run.id) == checkpoint

    step_id = uuid4()
    lease = await store.acquire_invocation(
        run_id=run.id,
        plan_step_id=step_id,
        tool_name="create_artifact",
        args={"path": "report.md"},
        worker_id="worker-a",
        lease_s=30,
    )
    assert lease.acquired
    with pytest.raises(InvocationInFlightError):
        await store.acquire_invocation(
            run_id=run.id,
            plan_step_id=step_id,
            tool_name="create_artifact",
            args={"path": "report.md"},
            worker_id="worker-b",
            lease_s=30,
        )
    await store.complete_invocation(
        key=lease.idempotency_key,
        worker_id="worker-a",
        result={"path": "report.md"},
        effect_ref="sha256:demo",
    )
    replay = await store.acquire_invocation(
        run_id=run.id,
        plan_step_id=step_id,
        tool_name="create_artifact",
        args={"path": "report.md"},
        worker_id="worker-b",
        lease_s=30,
    )
    assert replay.acquired is False
    assert replay.result == {"path": "report.md"}


async def test_jsonl_store_fsyncs_and_ignores_duplicate_or_broken_tail(tmp_path: Path) -> None:
    store = JsonlConversationStore(tmp_path / "conversations")
    await store.initialize()
    conversation_id = uuid4()
    message = JsonlMessage.create(
        conversation_id=conversation_id,
        seq=1,
        role="user",
        content="请检查当前目录",
    )

    await store.append(message)
    await store.append(message)
    path = tmp_path / "conversations" / f"{conversation_id}.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"broken":')

    recovered = await store.read(conversation_id)
    assert recovered == [message]
    assert path.stat().st_mode & 0o777 == 0o600


async def test_sqlite_conversation_archive_filters_and_restores(tmp_path: Path) -> None:
    store = SqliteCoworkStore(tmp_path / "state" / "cowork.db")
    await store.initialize()
    conversation_id = await store.create_conversation(title="待归档")

    assert [row["id"] for row in await store.list_conversation_metadata()] == [
        str(conversation_id)
    ]
    assert await store.set_conversation_archived(
        conversation_id=conversation_id, archived=True
    )
    assert await store.list_conversation_metadata() == []
    archived = await store.list_conversation_metadata(archived=True)
    assert [row["id"] for row in archived] == [str(conversation_id)]
    assert archived[0]["archived_at"] is not None

    assert await store.set_conversation_archived(
        conversation_id=conversation_id, archived=False
    )
    restored = await store.list_conversation_metadata()
    assert [row["id"] for row in restored] == [str(conversation_id)]
    assert restored[0]["archived_at"] is None


async def test_sqlite_conversation_delete_allows_unleased_waiting_run(tmp_path: Path) -> None:
    store = SqliteCoworkStore(tmp_path / "state" / "cowork.db")
    await store.initialize()
    conversation_id = await store.create_conversation(title="旧等待会话")
    run = await store.create_run(
        conversation_id=conversation_id,
        goal="等待用户补充",
        budget_tokens=1_000,
        budget_calls=10,
        budget_wall_ms=30_000,
        workflow_type="cowork",
    )
    assert await store.claim_run(run_id=run.id, worker_id="waiting-test", lease_s=60)
    with pytest.raises(ConversationBusyError, match="正在执行"):
        await store.delete_conversation(conversation_id=conversation_id)
    assert await store.set_run_waiting_human(run_id=run.id, worker_id="waiting-test")

    assert await store.delete_conversation(conversation_id=conversation_id)
    assert not await store.conversation_exists(conversation_id)


async def test_sqlite_store_covers_permissions_artifacts_inbox_and_scheduler(
    tmp_path: Path,
) -> None:
    store = SqliteCoworkStore(tmp_path / "state" / "cowork.db")
    await store.initialize()
    conversation_id = await store.create_conversation(title="迁移覆盖")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = await store.create_session_root(
        conversation_id=conversation_id,
        requested_path=str(workspace),
        access_mode="read_write",
    )
    assert (
        await store.authorize_path(
            conversation_id=conversation_id,
            target_path=workspace / "report.md",
            capability="filesystem.write",
        )
    ).root_id == root.id

    run = await store.create_run(
        conversation_id=conversation_id,
        goal="生成报告",
        budget_tokens=1000,
        budget_calls=10,
        budget_wall_ms=30_000,
    )
    artifact_path = workspace / "report.md"
    artifact_path.write_text("ok", encoding="utf-8")
    artifact = await store.register_artifact(
        conversation_id=conversation_id,
        run_id=run.id,
        session_root_id=root.id,
        kind="file",
        title="报告",
        uri=str(artifact_path),
        mime_type="text/markdown",
    )
    assert (await store.resolve_artifact_file(artifact_id=artifact.id))[1] == artifact_path

    step_id = uuid4()
    await store.upsert_plan_step(
        step_id=step_id,
        run_id=run.id,
        step_idx=0,
        description="等待确认",
        tool="ask_user",
        status="running",
    )
    inbox = await store.create_inbox_item(
        run_id=run.id,
        conversation_id=conversation_id,
        kind="ask_user",
        tool_call_id="call-1",
        plan_step_id=step_id,
        request={"question": "继续吗？"},
    )
    assert (
        await store.get_inbox_item(run_id=run.id, resume_token=inbox.resume_token)
    ).id == inbox.id

    now = datetime.now(UTC)
    schedule = await store.create_schedule(
        conversation_id=conversation_id,
        title="日报",
        goal="生成日报",
        schedule_kind="once",
        cron_expression=None,
        run_at=now + timedelta(hours=1),
        timezone="Asia/Shanghai",
        next_run_at=now + timedelta(hours=1),
    )
    assert (await store.get_schedule(schedule_id=schedule.id)).title == "日报"


async def test_sqlite_backend_routes_new_cowork_run_without_postgres_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        cowork_store_backend="sqlite",
        cowork_data_path=tmp_path / "data",
        cowork_default_workspace_path=tmp_path / "workspace",
    )
    await initialize_local_cowork_stores(settings)
    monkeypatch.setattr("app.cowork_store.routing.get_settings", lambda: settings)
    session = AsyncMock()
    try:
        conversation_id = await ensure_conversation(session, title="SQLite 会话")
        stores = await initialize_local_cowork_stores(settings)
        old_workspace = tmp_path / "old-default"
        old_workspace.mkdir()
        await stores.state.create_session_root(
            conversation_id=conversation_id,
            requested_path=str(old_workspace),
            access_mode="read_write",
            label=DEFAULT_WORKSPACE_LABEL,
        )
        await ensure_default_session_root(
            session,
            conversation_id=conversation_id,
            workspace_path=settings.cowork_default_workspace_path,
        )
        run = await create_run(
            session,
            conversation_id=conversation_id,
            goal="检查本地目录",
            budget_tokens=1000,
            budget_calls=10,
            budget_wall_ms=30_000,
            workflow_type="cowork",
        )
        await append_message(
            session,
            conversation_id=conversation_id,
            role="user",
            content=run.goal,
            run_id=run.id,
        )
        await initialize_cowork_state(
            session,
            run_id=run.id,
            registry=build_default_cowork_registry(),
            commit=False,
        )
        roots = await stores.state.list_session_roots(conversation_id=conversation_id)
        assert roots[0].canonical_path == str(settings.cowork_default_workspace_path)
        assert all(root.canonical_path != str(old_workspace) for root in roots)
        assert await stores.state.load_latest_checkpoint(run_id=run.id) is not None
        assert [event.type for event in await stores.state.list_events(run_id=run.id)] == ["plan"]
        session.execute.assert_not_awaited()
    finally:
        await close_local_cowork_stores()


async def test_sqlite_watchdog_also_reaps_postgres_answer_runs(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(cowork_store_backend="sqlite", cowork_data_path=tmp_path / "data")
    stores = await initialize_local_cowork_stores(settings)
    monkeypatch.setattr("app.cowork_store.routing.get_settings", lambda: settings)
    try:
        local_conversation = await stores.state.create_conversation(title="local")
        local_run = await stores.state.create_run(
            conversation_id=local_conversation,
            goal="恢复本地任务",
            budget_tokens=1000,
            budget_calls=10,
            budget_wall_ms=30_000,
        )
        await stores.state.save_checkpoint(
            run_id=local_run.id,
            state={"status": "executing", "messages": []},
            parent_id=None,
        )
        assert await stores.state.claim_run(
            run_id=local_run.id, worker_id="lost-local", lease_s=30
        )
        await stores.state._write(  # 故障注入必须制造过期租约
            lambda connection: connection.execute(
                "UPDATE agent_runs SET lease_until = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), str(local_run.id)),
            )
        )

        pg_conversation = uuid4()
        await db_session.execute(
            text(
                """INSERT INTO conversations(id, scope, title)
                   VALUES (:id, 'local_owner', 'postgres answer')"""
            ),
            {"id": pg_conversation},
        )
        answer_run = await create_run(
            db_session,
            conversation_id=pg_conversation,
            goal="回答旧问题",
            budget_tokens=1000,
            budget_calls=10,
            budget_wall_ms=30_000,
            workflow_type="answer",
        )
        await db_session.execute(
            text(
                """UPDATE agent_runs SET status = 'executing', worker_id = 'lost-pg',
                          lease_until = now() - interval '1 second'
                   WHERE id = :id"""
            ),
            {"id": answer_run.id},
        )

        reaped = await reap_expired_runs(db_session)

        assert reaped.recovered_cowork == [(local_run.id, 1)]
        assert answer_run.id in reaped.failed
    finally:
        await close_local_cowork_stores()


async def test_sqlite_history_loaded_prevents_cross_turn_duplication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(cowork_store_backend="sqlite", cowork_data_path=tmp_path / "data")
    stores = await initialize_local_cowork_stores(settings)
    monkeypatch.setattr("app.cowork_store.routing.get_settings", lambda: settings)
    session = AsyncMock()
    registry = build_default_cowork_registry()
    try:
        conversation_id = await ensure_conversation(session, title="多轮历史")
        for turn in (1, 2):
            run = await create_run(
                session,
                conversation_id=conversation_id,
                goal=f"问题{turn}",
                budget_tokens=1000,
                budget_calls=10,
                budget_wall_ms=30_000,
                workflow_type="cowork",
            )
            await append_message(
                session,
                conversation_id=conversation_id,
                role="user",
                content=f"问题{turn}",
                run_id=run.id,
            )
            state = await initialize_cowork_state(
                session, run_id=run.id, registry=registry, commit=False
            )
            state["messages"].append({"role": "assistant", "content": f"回答{turn}"})
            latest = await stores.state.load_latest_checkpoint(run_id=run.id)
            assert latest is not None
            await stores.state.save_checkpoint(
                run_id=run.id,
                state=dict(state),
                parent_id=latest.checkpoint_id,
            )
            await append_message(
                session,
                conversation_id=conversation_id,
                role="assistant",
                content=f"回答{turn}",
                run_id=run.id,
            )
            assert await finish_run(session, run_id=run.id, status="done")

        third = await create_run(
            session,
            conversation_id=conversation_id,
            goal="问题3",
            budget_tokens=1000,
            budget_calls=10,
            budget_wall_ms=30_000,
            workflow_type="cowork",
        )
        await append_message(
            session,
            conversation_id=conversation_id,
            role="user",
            content="问题3",
            run_id=third.id,
        )
        third_state = await initialize_cowork_state(
            session, run_id=third.id, registry=registry, commit=False
        )
        contents = [str(message.get("content", "")) for message in third_state["messages"]]

        assert contents == ["问题1", "回答1", "问题2", "回答2", "问题3"]
        session.execute.assert_not_awaited()
    finally:
        await close_local_cowork_stores()


async def test_sqlite_get_conversation_is_not_limited_to_latest_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(cowork_store_backend="sqlite", cowork_data_path=tmp_path / "data")
    stores = await initialize_local_cowork_stores(settings)
    monkeypatch.setattr("app.cowork_store.routing.get_settings", lambda: settings)
    session = AsyncMock()
    try:
        oldest = await stores.state.create_conversation(title="最早会话")
        for index in range(205):
            await stores.state.create_conversation(title=f"会话 {index}")

        found = await get_conversation(
            session,
            conversation_id=oldest,
            scope="local_owner",
            demo_session_id=None,
        )

        assert found is not None
        assert found.id == oldest
        assert found.title == "最早会话"
        session.execute.assert_not_awaited()
    finally:
        await close_local_cowork_stores()


async def test_default_workspace_rejects_application_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CoworkPermissionError, match="项目或应用工作目录"):
        await ensure_default_session_root(
            AsyncMock(),
            conversation_id=uuid4(),
            workspace_path=tmp_path,
        )
