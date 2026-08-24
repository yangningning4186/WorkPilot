from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.agent_core.idempotency import InvocationInFlightError
from app.core.config import Settings
from app.core.queue import InProcessRunQueue, QueuedTask
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
)
from app.worker.local_runtime import EmbeddedWorkerRuntime


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


async def test_in_process_queue_keeps_user_runs_separate_from_background_jobs() -> None:
    queue = InProcessRunQueue()
    skill_id = uuid4()
    memory_id = uuid4()
    run_id = uuid4()

    await queue.enqueue_skill_job(skill_id)
    await queue.enqueue_memory_job(memory_id)
    await queue.enqueue_cowork_run(run_id)

    foreground = await queue.get_foreground()
    first_background = await queue.get_background()
    second_background = await queue.get_background()

    assert (foreground.name, foreground.object_id) == ("cowork_run", run_id)
    assert (first_background.name, first_background.object_id) == (
        "memory_extraction_job",
        memory_id,
    )
    assert (second_background.name, second_background.object_id) == (
        "skill_distillation_job",
        skill_id,
    )
    for task in (foreground, first_background, second_background):
        queue.task_done(task)


async def test_embedded_background_waits_until_foreground_is_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = InProcessRunQueue()
    runtime = EmbeddedWorkerRuntime(Settings(cowork_dispatch_poll_s=0.01), queue)
    foreground_started = asyncio.Event()
    release_foreground = asyncio.Event()
    background_started = asyncio.Event()

    async def execute(task: QueuedTask) -> None:
        if task.name == "cowork_run":
            foreground_started.set()
            await release_foreground.wait()
        else:
            background_started.set()

    monkeypatch.setattr(runtime, "_execute", execute)
    await queue.enqueue_skill_job(uuid4())
    await queue.enqueue_cowork_run(uuid4())
    foreground_consumer = asyncio.create_task(runtime._consume_foreground())
    background_consumer = asyncio.create_task(runtime._consume_background())
    try:
        await asyncio.wait_for(foreground_started.wait(), timeout=1)
        await asyncio.sleep(0.05)
        assert not background_started.is_set()

        release_foreground.set()
        await asyncio.wait_for(background_started.wait(), timeout=1)
    finally:
        foreground_consumer.cancel()
        background_consumer.cancel()
        await asyncio.gather(
            foreground_consumer,
            background_consumer,
            return_exceptions=True,
        )


async def test_running_background_job_cannot_starve_a_new_user_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = InProcessRunQueue()
    runtime = EmbeddedWorkerRuntime(Settings(cowork_dispatch_poll_s=0.01), queue)
    background_started = asyncio.Event()
    release_background = asyncio.Event()
    foreground_started = asyncio.Event()

    async def execute(task: QueuedTask) -> None:
        if task.name == "skill_distillation_job":
            background_started.set()
            await release_background.wait()
        else:
            foreground_started.set()

    monkeypatch.setattr(runtime, "_execute", execute)
    await queue.enqueue_skill_job(uuid4())
    background_consumer = asyncio.create_task(runtime._consume_background())
    foreground_consumer = asyncio.create_task(runtime._consume_foreground())
    try:
        await asyncio.wait_for(background_started.wait(), timeout=1)
        await queue.enqueue_cowork_run(uuid4())
        await asyncio.wait_for(foreground_started.wait(), timeout=1)
        assert not release_background.is_set()
    finally:
        release_background.set()
        foreground_consumer.cancel()
        background_consumer.cancel()
        await asyncio.gather(
            foreground_consumer,
            background_consumer,
            return_exceptions=True,
        )


async def test_generated_conversation_title_uses_compare_and_set(tmp_path: Path) -> None:
    store = SqliteCoworkStore(tmp_path / "titles" / "cowork.db")
    await store.initialize()
    conversation_id = await store.create_conversation(title="新会话")
    before = await store.list_conversation_metadata(
        conversation_id=conversation_id,
        archived=None,
        limit=1,
    )

    assert await store.compare_and_set_conversation_title(
        conversation_id=conversation_id,
        expected_title="新会话",
        title="论文方法与证据链",
    )
    assert not await store.compare_and_set_conversation_title(
        conversation_id=conversation_id,
        expected_title="新会话",
        title="迟到的标题",
    )
    metadata = await store.list_conversation_metadata(
        conversation_id=conversation_id,
        archived=None,
        limit=1,
    )
    assert metadata[0]["title"] == "论文方法与证据链"
    assert metadata[0]["updated_at"] == before[0]["updated_at"]
    await store.close()


async def test_conversation_metadata_is_projected_without_history_rescans(
    tmp_path: Path,
) -> None:
    """列表摘要与活跃任务在一条查询里给出；最新终态不能遮住更早的活跃任务。"""

    store = SqliteCoworkStore(tmp_path / "metadata" / "cowork.db")
    await store.initialize()
    conversation_id = await store.create_conversation(title="性能检查")
    await store.allocate_message(
        record_id=uuid4(),
        conversation_id=conversation_id,
        role="user",
        status="completed",
        run_id=None,
        title_source="这是一条会话列表摘要",
    )
    active = await store.create_run(
        conversation_id=conversation_id,
        goal="仍在运行",
        budget_tokens=100,
        budget_calls=1,
        budget_wall_ms=1_000,
    )
    newer = await store.create_run(
        conversation_id=conversation_id,
        goal="后来但已结束",
        budget_tokens=100,
        budget_calls=1,
        budget_wall_ms=1_000,
    )
    assert await store.finish_run(run_id=newer.id, status="done")

    rows = await store.list_conversation_metadata(
        conversation_id=conversation_id,
        archived=None,
        limit=1,
    )

    assert rows[0]["message_count"] == 1
    assert rows[0]["latest_message"] == "这是一条会话列表摘要"
    assert rows[0]["last_message_at"] is not None
    assert rows[0]["active_run_id"] == str(active.id)
    assert {run.id for run in await store.get_runs((active.id, newer.id, active.id))} == {
        active.id,
        newer.id,
    }
    await store.close()


async def test_initializing_run_is_not_dispatchable_until_checkpoint_and_events_commit(
    tmp_path: Path,
) -> None:
    store = SqliteCoworkStore(tmp_path / "initializing" / "cowork.db")
    await store.initialize()
    conversation_id = await store.create_conversation(title="初始化事务")
    run = await store.create_run(
        conversation_id=conversation_id,
        goal="先完整初始化再执行",
        budget_tokens=100,
        budget_calls=2,
        budget_wall_ms=1_000,
        initializing=True,
    )

    assert run.status == "initializing"
    assert await store.list_queued_runs() == []
    assert await store.conversation_has_active_run(conversation_id=conversation_id)

    activated, checkpoint, events = await store.initialize_run(
        run_id=run.id,
        checkpoint_id="initial-checkpoint",
        state={"schema_version": "cowork.v2", "status": "executing"},
        events=[("plan", {"mode": "dynamic_tool_loop"})],
    )

    assert activated.status == "queued"
    assert checkpoint.checkpoint_id == "initial-checkpoint"
    assert [event.type for event in events] == ["plan"]
    assert [item.id for item in await store.list_queued_runs()] == [run.id]
    assert await store.load_latest_checkpoint(run_id=run.id) == checkpoint
    await store.close()


async def test_initializing_run_rolls_back_checkpoint_status_and_events_together(
    tmp_path: Path,
) -> None:
    store = SqliteCoworkStore(tmp_path / "initializing-rollback" / "cowork.db")
    await store.initialize()
    conversation_id = await store.create_conversation(title="初始化回滚")
    run = await store.create_run(
        conversation_id=conversation_id,
        goal="事件失败时不能进入队列",
        budget_tokens=100,
        budget_calls=2,
        budget_wall_ms=1_000,
        initializing=True,
    )

    with pytest.raises((TypeError, ValueError)):
        await store.initialize_run(
            run_id=run.id,
            checkpoint_id="must-roll-back",
            state={"schema_version": "cowork.v2", "status": "executing"},
            events=[("plan", {"not_json": object()})],
        )

    refreshed = await store.get_run(run.id)
    assert refreshed is not None and refreshed.status == "initializing"
    assert await store.load_latest_checkpoint(run_id=run.id) is None
    assert await store.list_events(run_id=run.id) == []
    assert await store.list_queued_runs() == []
    await store.close()


async def test_restart_marks_interrupted_initialization_failed(tmp_path: Path) -> None:
    database = tmp_path / "initializing-restart" / "cowork.db"
    first = SqliteCoworkStore(database)
    await first.initialize()
    conversation_id = await first.create_conversation(title="中断初始化")
    run = await first.create_run(
        conversation_id=conversation_id,
        goal="模拟 sidecar 在初始化中退出",
        budget_tokens=100,
        budget_calls=2,
        budget_wall_ms=1_000,
        initializing=True,
    )
    await first.close()

    reopened = SqliteCoworkStore(database)
    await reopened.initialize()
    recovered = await reopened.get_run(run.id)
    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.error == "run initialization interrupted"
    assert not await reopened.conversation_has_active_run(conversation_id=conversation_id)
    await reopened.close()


async def test_checkpoint_usage_events_and_pause_are_one_transaction(tmp_path: Path) -> None:
    store = SqliteCoworkStore(tmp_path / "checkpoint-transaction" / "cowork.db")
    await store.initialize()
    conversation_id = await store.create_conversation(title="Checkpoint 事务")
    run = await store.create_run(
        conversation_id=conversation_id,
        goal="等待用户",
        budget_tokens=100,
        budget_calls=2,
        budget_wall_ms=1_000,
    )
    assert await store.claim_run(run_id=run.id, worker_id="worker-a", lease_s=30)

    checkpoint, events = await store.commit_checkpoint(
        run_id=run.id,
        checkpoint_id="pause-checkpoint",
        parent_id=None,
        state={"schema_version": "cowork.v2", "status": "waiting_human"},
        used_tokens=17,
        used_calls=1,
        events=[("interrupt", {"kind": "ask_user"})],
        worker_id="worker-a",
        transition_to="waiting_human",
    )

    refreshed = await store.get_run(run.id)
    assert refreshed is not None
    assert refreshed.status == "waiting_human"
    assert refreshed.worker_id is None
    assert refreshed.used_tokens == 17
    assert refreshed.used_calls == 1
    assert await store.load_latest_checkpoint(run_id=run.id) == checkpoint
    assert [event.type for event in events] == ["interrupt"]
    assert [event.type for event in await store.list_events(run_id=run.id)] == ["interrupt"]
    await store.close()


async def test_checkpoint_transaction_rolls_back_on_event_encoding_failure(
    tmp_path: Path,
) -> None:
    store = SqliteCoworkStore(tmp_path / "checkpoint-rollback" / "cowork.db")
    await store.initialize()
    conversation_id = await store.create_conversation(title="Checkpoint 回滚")
    run = await store.create_run(
        conversation_id=conversation_id,
        goal="失败不能留下半份状态",
        budget_tokens=100,
        budget_calls=2,
        budget_wall_ms=1_000,
    )
    assert await store.claim_run(run_id=run.id, worker_id="worker-a", lease_s=30)

    with pytest.raises((TypeError, ValueError)):
        await store.commit_checkpoint(
            run_id=run.id,
            checkpoint_id="must-roll-back",
            parent_id=None,
            state={"schema_version": "cowork.v2", "status": "waiting_human"},
            used_tokens=17,
            used_calls=1,
            events=[("interrupt", {"not_json": object()})],
            worker_id="worker-a",
            transition_to="waiting_human",
        )

    refreshed = await store.get_run(run.id)
    assert refreshed is not None
    assert refreshed.status == "executing"
    assert refreshed.worker_id == "worker-a"
    assert refreshed.used_tokens == 0
    assert refreshed.used_calls == 0
    assert await store.load_latest_checkpoint(run_id=run.id) is None
    assert await store.list_events(run_id=run.id) == []
    await store.close()


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
    assert [event.seq for event in await store.list_events(run_id=run.id, limit=1)] == [1]
    assert [
        event.seq for event in await store.list_events(run_id=run.id, after_seq=1, limit=1)
    ] == [2]

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

    assert [row["id"] for row in await store.list_conversation_metadata()] == [str(conversation_id)]
    assert await store.set_conversation_archived(conversation_id=conversation_id, archived=True)
    assert await store.list_conversation_metadata() == []
    archived = await store.list_conversation_metadata(archived=True)
    assert [row["id"] for row in archived] == [str(conversation_id)]
    assert archived[0]["archived_at"] is not None

    assert await store.set_conversation_archived(conversation_id=conversation_id, archived=False)
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

    artifact_path.write_text("updated", encoding="utf-8")
    latest_artifact = await store.register_artifact(
        conversation_id=conversation_id,
        run_id=run.id,
        session_root_id=root.id,
        kind="file",
        title="报告",
        uri=str(artifact_path),
        mime_type="text/markdown",
        meta={"sha256": "new-version"},
    )
    # 会话右栏按真实文件去重，只展示同一路径的最新版本；run 级审计仍保留两次登记。
    assert [item.id for item in await store.list_artifacts(conversation_id=conversation_id)] == [
        latest_artifact.id
    ]
    assert [item.id for item in await store.list_run_artifacts(run_id=run.id)] == [
        artifact.id,
        latest_artifact.id,
    ]

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


async def test_sqlite_store_matches_postgres_memory_semantics(tmp_path: Path) -> None:
    """SQLite 后端必须和 PostgreSQL 同语义：同 key 更新、软删除、按作用域可见。"""

    store = SqliteCoworkStore(tmp_path / "state" / "cowork.db")
    await store.initialize()
    mine = await store.create_conversation(title="记忆本会话")
    theirs = await store.create_conversation(title="记忆别的会话")
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()

    first, replaced = await store.remember_cowork_memory(
        scope="global",
        conversation_id=None,
        workspace_path=None,
        key="report-format",
        content="用户偏好 PDF",
        source="agent",
    )
    assert replaced is None
    second, previous = await store.remember_cowork_memory(
        scope="global",
        conversation_id=None,
        workspace_path=None,
        key="report-format",
        content="用户偏好 Markdown",
        source="agent",
    )
    assert second.id == first.id
    assert previous is not None and previous.content == "用户偏好 PDF"

    for scope, conversation, path, content in (
        ("conversation", mine, None, "本会话笔记"),
        ("conversation", theirs, None, "别的会话笔记"),
        ("workspace", None, str(workspace), "本目录约定"),
        ("workspace", None, str(other_workspace), "别的目录约定"),
    ):
        await store.remember_cowork_memory(
            scope=scope,
            conversation_id=conversation,
            workspace_path=path,
            key=None,
            content=content,
            source="agent",
        )

    visible = await store.list_cowork_memories(
        conversation_id=mine,
        workspace_paths=[str(workspace)],
        include_forgotten=False,
        limit=100,
    )
    assert {item.content for item in visible} == {
        "用户偏好 Markdown",
        "本会话笔记",
        "本目录约定",
    }

    assert (await store.forget_cowork_memory(memory_id=second.id)) is not None
    # 重复 forget 是幂等的，不是错误。
    assert (await store.forget_cowork_memory(memory_id=second.id)) is None
    active = await store.list_cowork_memories(
        conversation_id=mine, workspace_paths=[], include_forgotten=False, limit=100
    )
    assert second.id not in {item.id for item in active}

    restored, _ = await store.update_cowork_memory(memory_id=second.id, content=None, restore=True)
    assert restored.forgotten_at is None
    assert (await store.get_cowork_memory(memory_id=second.id)).content == "用户偏好 Markdown"


async def test_sqlite_backend_routes_new_cowork_run_without_postgres_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        cowork_store_backend="sqlite",
        cowork_data_path=tmp_path / "data",
        cowork_default_workspace_path=tmp_path / "workspace",
    )
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


async def test_sqlite_history_loaded_prevents_cross_turn_duplication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(cowork_store_backend="sqlite", cowork_data_path=tmp_path / "data")
    stores = await initialize_local_cowork_stores(settings)
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


async def test_failed_checkpoint_internal_messages_do_not_pollute_the_next_run(
    tmp_path: Path,
) -> None:
    settings = Settings(cowork_store_backend="sqlite", cowork_data_path=tmp_path / "data")
    stores = await initialize_local_cowork_stores(settings)
    session = AsyncMock()
    registry = build_default_cowork_registry()
    try:
        conversation_id = await ensure_conversation(session, title="失败历史隔离")
        first = await create_run(
            session,
            conversation_id=conversation_id,
            goal="hello",
            budget_tokens=1000,
            budget_calls=10,
            budget_wall_ms=30_000,
            workflow_type="cowork",
        )
        await append_message(
            session,
            conversation_id=conversation_id,
            role="user",
            content=first.goal,
            run_id=first.id,
        )
        state = await initialize_cowork_state(session, run_id=first.id, registry=registry)
        state["messages"].extend(
            [
                {"role": "assistant", "content": "未通过校验的草稿"},
                {"role": "system", "content": "<citation_repair>内部重试</citation_repair>"},
            ]
        )
        latest = await stores.state.load_latest_checkpoint(run_id=first.id)
        assert latest is not None
        await stores.state.save_checkpoint(
            run_id=first.id,
            state=dict(state),
            parent_id=latest.checkpoint_id,
        )
        await append_message(
            session,
            conversation_id=conversation_id,
            role="assistant",
            content="执行失败",
            status="failed",
            run_id=first.id,
        )
        assert await finish_run(session, run_id=first.id, status="failed", error="provider 400")

        second = await create_run(
            session,
            conversation_id=conversation_id,
            goal="你好",
            budget_tokens=1000,
            budget_calls=10,
            budget_wall_ms=30_000,
            workflow_type="cowork",
        )
        await append_message(
            session,
            conversation_id=conversation_id,
            role="user",
            content=second.goal,
            run_id=second.id,
        )
        next_state = await initialize_cowork_state(session, run_id=second.id, registry=registry)

        assert next_state["messages"] == [
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "你好"},
        ]
    finally:
        await close_local_cowork_stores()


async def test_sqlite_get_conversation_is_not_limited_to_latest_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(cowork_store_backend="sqlite", cowork_data_path=tmp_path / "data")
    stores = await initialize_local_cowork_stores(settings)
    session = AsyncMock()
    try:
        oldest = await stores.state.create_conversation(title="最早会话")
        for index in range(205):
            await stores.state.create_conversation(title=f"会话 {index}")

        found = await get_conversation(
            session,
            conversation_id=oldest,
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


async def test_conversation_kb_binding_round_trips_and_survives_an_upgrade(
    tmp_path: Path,
) -> None:
    """挂载是会话级的持久状态，不是请求参数。

    顺带钉住旧库升级：`kb_slug` 是后加的列，`_initialize_sync` 必须能在已有库上补上它，
    否则升级之后每次挂载都会撞 "no such column"。
    """
    path = tmp_path / "state" / "cowork.db"
    store = SqliteCoworkStore(path)
    await store.initialize()
    conversation_id = await store.create_conversation(title="论文问答")

    assert await store.get_conversation_kb(conversation_id=conversation_id) is None
    assert await store.set_conversation_kb(conversation_id=conversation_id, kb_slug="papers")
    assert await store.get_conversation_kb(conversation_id=conversation_id) == "papers"

    # 卸载。
    assert await store.set_conversation_kb(conversation_id=conversation_id, kb_slug=None)
    assert await store.get_conversation_kb(conversation_id=conversation_id) is None

    # 不存在的会话不该被当成"挂上了"。
    assert not await store.set_conversation_kb(conversation_id=uuid4(), kb_slug="papers")

    # 再次 initialize 等价于对一个已有库跑升级：不能重复 ALTER，也不能丢数据。
    await store.set_conversation_kb(conversation_id=conversation_id, kb_slug="papers")
    reopened = SqliteCoworkStore(path)
    await reopened.initialize()
    assert await reopened.get_conversation_kb(conversation_id=conversation_id) == "papers"


async def test_sqlite_store_upgrades_memory_indexes_after_adding_validity_columns(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state" / "cowork.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE cowork_memories (
                id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                conversation_id TEXT,
                workspace_path TEXT,
                key TEXT,
                content TEXT NOT NULL,
                forgotten_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """INSERT INTO cowork_memories (
                id, scope, key, content, created_at, updated_at
            ) VALUES ('legacy', 'global', 'language', '中文', '2026-01-01', '2026-01-01')"""
        )

    await SqliteCoworkStore(path).initialize()

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(cowork_memories)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(cowork_memories)")}
        valid_from = connection.execute(
            "SELECT valid_from FROM cowork_memories WHERE id = 'legacy'"
        ).fetchone()

    assert {"valid_from", "invalid_at", "superseded_by"} <= columns
    assert {
        "ix_local_cowork_memories_active",
        "ix_local_cowork_memories_history",
        "uq_local_cowork_memories_key",
    } <= indexes
    assert valid_from == ("2026-01-01",)
