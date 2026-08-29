from __future__ import annotations

import asyncio
import json
import sqlite3
from itertools import pairwise
from pathlib import Path
from uuid import UUID

import pytest
from uuid6 import uuid7

from app.cowork.team_wake import TEAM_WAKE_CONSUMER, dispatch_team_wakes_once
from app.cowork_contracts import (
    TeamBudgetExceededError,
    TeamEventIntegrityError,
    TeamRecord,
    TeamWorkerRecord,
)
from app.cowork_store.sqlite import SqliteCoworkStore


def _team_receipt() -> dict[str, object]:
    return {
        "receipt_id": "team-write-receipt",
        "mechanism": "team_write_delegation_approval",
        "scope_sha256": "team-scope-sha",
        "approval_inbox_id": "human-approval-inbox",
    }


def _task_receipt() -> dict[str, object]:
    return {
        "receipt_id": "task-scope-receipt",
        "mechanism": "team_board_write_scope",
        "scope_sha256": "task-scope-sha",
        "delegation_receipt_id": "team-write-receipt",
    }


async def _create_team(
    store: SqliteCoworkStore, *, with_receipt: bool = False
) -> tuple[UUID, TeamRecord, TeamWorkerRecord]:
    conversation_id = await store.create_conversation(title="event sourced team")
    receipt = _team_receipt() if with_receipt else None
    team, workers = await store.create_team(
        lead_conversation_id=conversation_id,
        proposal_call_id="proposal-call",
        note="event test",
        members=[
            {
                "name": "worker",
                "role": "test event order",
                "reason": "independent verification",
                "state": {"status": "idle", "messages": []},
            }
        ],
        write_delegation_scope=(
            [{"path": "/tmp/team-events", "access_mode": "read_write"}] if with_receipt else []
        ),
        write_delegation_receipt=receipt,
        event_actor="human:user",
        event_cause="proposal-call",
    )
    return conversation_id, team, workers[0]


async def test_team_event_log_orders_receipts_and_task_lifecycle_and_rebuilds(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cowork.db"
    store = SqliteCoworkStore(path)
    await store.initialize()
    conversation_id, team, worker = await _create_team(store, with_receipt=True)
    task = await store.create_board_task(
        lead_conversation_id=conversation_id,
        title="write report",
        description="produce a deterministic report",
        acceptance_criteria="report is complete",
        resource_scope=[{"path": "/tmp/team-events", "access_mode": "read_write"}],
        scope_receipt=_task_receipt(),
        event_actor=f"lead:{conversation_id}",
        event_cause="create-call",
    )
    started, _, session = await store.start_board_task(
        lead_conversation_id=conversation_id,
        task_id=task.id,
        worker_name=worker.name,
        assignment_call_id="assign-call",
        event_actor=f"lead:{conversation_id}",
        event_cause="assign-call",
    )
    await store.save_team_worker_session(
        session_id=session.id,
        task_id=task.id,
        state={"status": "active", "rounds_used": 1},
        event_actor=f"worker:{worker.id}",
        event_cause="assign-call",
    )
    submitted = await store.complete_board_task(
        session_id=session.id,
        task_id=task.id,
        state={"status": "idle", "rounds_used": 1},
        worker_report="complete",
        event_actor=f"worker:{worker.id}",
        event_cause="assign-call",
    )
    assert (started.status, submitted.status) == ("in_progress", "review")
    done = await store.review_board_task(
        lead_conversation_id=conversation_id,
        task_id=task.id,
        accepted=True,
        feedback="accepted",
        event_actor=f"lead:{conversation_id}",
        event_cause="review-call",
    )
    assert done.status == "done"

    events = await store.list_team_events(team_id=team.id)
    assert [event.event_type for event in events] == [
        "team.created",
        "team.write_delegation_receipt_minted",
        "board.task.created",
        "board.task.scope_receipt_minted",
        "team.budget_reserved",
        "board.task.assigned",
        "team.worker_session.checkpointed",
        "board.task.submitted",
        "team.budget_settled",
        "board.task.reviewed",
    ]
    assert [event.sequence for event in events] == list(range(1, 11))
    assert events[0].actor == "human:user"
    assert events[0].cause == "proposal-call"
    assert events[0].parent_event_id is None
    for previous, current in pairwise(events):
        assert current.parent_event_id == previous.id
        assert current.prev_hash == previous.hash
        assert len(current.hash) == 64

    verification = await store.verify_team_event_log(team_id=team.id)
    assert verification.valid is True
    assert (verification.event_count, verification.head_sequence) == (10, 10)
    assert verification.head_hash == events[-1].hash

    replayed = await store.replay_team_event_projection(team_id=team.id)
    rebuilt = await store.rebuild_team_event_projection(team_id=team.id)
    assert replayed.summary == rebuilt.summary
    assert replayed.summary["team"]["id"] == str(team.id)
    assert replayed.summary["tasks"][0]["status"] == "done"
    assert replayed.summary["worker_checkpoint_count"] == 1
    assert replayed.summary["workers"][0]["session_status"] == "idle"
    assert replayed.summary["workers"][0]["active_task_id"] is None
    assert {item["kind"] for item in replayed.summary["receipts"]} == {
        "team_write",
        "task_scope",
    }
    with sqlite3.connect(path) as connection:
        projection = connection.execute(
            """SELECT watermark, head_hash, summary
               FROM cowork_team_event_projection_summaries WHERE team_id = ?""",
            (str(team.id),),
        ).fetchone()
    assert projection is not None
    assert projection[0:2] == (10, events[-1].hash)
    assert json.loads(projection[2]) == rebuilt.summary


async def test_team_event_sequence_is_contiguous_under_concurrent_task_creates(
    tmp_path: Path,
) -> None:
    store = SqliteCoworkStore(tmp_path / "cowork.db")
    await store.initialize()
    conversation_id, team, _ = await _create_team(store)

    async def create(index: int) -> None:
        await store.create_board_task(
            lead_conversation_id=conversation_id,
            title=f"task-{index}",
            description="",
            acceptance_criteria="created",
            resource_scope=[],
            event_actor=f"lead:{conversation_id}",
            event_cause=f"create-{index}",
        )

    await asyncio.gather(*(create(index) for index in range(20)))
    events = await store.list_team_events(team_id=team.id, limit=100)
    assert len(events) == 21
    assert [event.sequence for event in events] == list(range(1, 22))
    assert sum(event.event_type == "board.task.created" for event in events) == 20
    assert (await store.verify_team_event_log(team_id=team.id)).event_count == 21


@pytest.mark.parametrize("tamper", ["payload", "sequence", "head", "tail_delete"])
async def test_team_event_verify_detects_out_of_band_tampering(tmp_path: Path, tamper: str) -> None:
    path = tmp_path / tamper / "cowork.db"
    store = SqliteCoworkStore(path)
    await store.initialize()
    _, team, _ = await _create_team(store, with_receipt=True)

    with sqlite3.connect(path) as connection:
        if tamper == "payload":
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(
                    "UPDATE cowork_team_events SET payload = '{}' WHERE sequence = 1"
                )
            connection.execute("DROP TRIGGER trg_local_team_events_no_update")
            connection.execute("UPDATE cowork_team_events SET payload = '{}' WHERE sequence = 1")
        elif tamper == "sequence":
            connection.execute("DROP TRIGGER trg_local_team_events_no_update")
            connection.execute("UPDATE cowork_team_events SET sequence = 9 WHERE sequence = 1")
        elif tamper == "head":
            connection.execute(
                "UPDATE cowork_team_event_heads SET head_hash = ? WHERE team_id = ?",
                ("f" * 64, str(team.id)),
            )
        else:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(
                    "DELETE FROM cowork_team_events WHERE team_id = ? AND sequence = 2",
                    (str(team.id),),
                )
            connection.execute("DROP TRIGGER trg_local_team_events_no_delete")
            connection.execute(
                "DELETE FROM cowork_team_events WHERE team_id = ? AND sequence = 2",
                (str(team.id),),
            )

    with pytest.raises(TeamEventIntegrityError):
        await store.verify_team_event_log(team_id=team.id)


async def test_v16_team_projection_migrates_to_replayable_v18_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cowork.db"
    original = SqliteCoworkStore(path)
    await original.initialize()
    conversation_id, team, _ = await _create_team(original, with_receipt=True)
    task = await original.create_board_task(
        lead_conversation_id=conversation_id,
        title="legacy open task",
        description="",
        acceptance_criteria="migrated",
        resource_scope=[],
    )

    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TRIGGER trg_local_team_events_no_update;
            DROP TRIGGER trg_local_team_events_no_delete;
            DROP TABLE cowork_team_event_projection_summaries;
            DROP TABLE cowork_team_event_cursors;
            DROP TABLE cowork_team_event_heads;
            DROP TABLE cowork_team_wake_outbox;
            DROP TABLE cowork_team_events;
            PRAGMA user_version = 16;
            """
        )

    migrated = SqliteCoworkStore(path)
    await migrated.initialize()
    events = await migrated.list_team_events(team_id=team.id)
    assert [(event.sequence, event.event_type) for event in events] == [
        (1, "team.projection_imported"),
        (2, "team.budget_initialized"),
    ]
    assert (events[0].actor, events[0].cause) == ("system:migration", "schema:v17")
    replayed = await migrated.replay_team_event_projection(team_id=team.id)
    assert replayed.summary["team"]["id"] == str(team.id)
    assert replayed.summary["tasks"][0]["id"] == str(task.id)
    assert replayed.summary["tasks"][0]["status"] == "open"
    assert replayed.summary["receipts"][0]["receipt_id"] == "team-write-receipt"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 26


async def test_v18_migration_failure_keeps_old_version_and_retries_atomically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cowork.db"
    original = SqliteCoworkStore(path)
    await original.initialize()
    _, team, _ = await _create_team(original)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA user_version = 17;
            CREATE TRIGGER fail_v18_budget_event
            BEFORE INSERT ON cowork_team_events
            WHEN NEW.event_type = 'team.budget_initialized'
            BEGIN
                SELECT RAISE(ABORT, 'simulated v18 migration crash');
            END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="simulated v18 migration crash"):
        await SqliteCoworkStore(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 17
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM cowork_team_events WHERE team_id = ? "
                "AND event_type = 'team.budget_initialized'",
                (str(team.id),),
            ).fetchone()[0]
            == 0
        )
        connection.execute("DROP TRIGGER fail_v18_budget_event")

    retried = SqliteCoworkStore(path)
    await retried.initialize()
    events = await retried.list_team_events(team_id=team.id)
    assert events[-1].event_type == "team.budget_initialized"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 26
        wake_rows = connection.execute(
            """SELECT event_sequence, event_type, status, delivery_receipt
               FROM cowork_team_wake_outbox WHERE team_id = ? ORDER BY event_sequence""",
            (str(team.id),),
        ).fetchall()
        cursor = connection.execute(
            """SELECT last_sequence FROM cowork_team_event_cursors
               WHERE team_id = ? AND consumer = ?""",
            (str(team.id), TEAM_WAKE_CONSUMER),
        ).fetchone()
    assert all(
        status == "delivered" and receipt == "migration:suppressed-existing"
        for _, _, status, receipt in wake_rows[:-1]
    )
    assert wake_rows[-1][1:] == ("team.budget_initialized", "pending", None)
    assert cursor == (wake_rows[-1][0] - 1,)


async def test_projection_and_event_batch_roll_back_when_receipt_event_append_fails(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cowork.db"
    store = SqliteCoworkStore(path)
    await store.initialize()
    conversation_id, team, _ = await _create_team(store, with_receipt=True)
    before = await store.list_team_events(team_id=team.id)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TRIGGER fail_scope_receipt_event
               BEFORE INSERT ON cowork_team_events
               WHEN NEW.event_type = 'board.task.scope_receipt_minted'
               BEGIN
                   SELECT RAISE(ABORT, 'simulated event append crash');
               END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="simulated event append crash"):
        await store.create_board_task(
            lead_conversation_id=conversation_id,
            title="must roll back",
            description="",
            acceptance_criteria="no partial write",
            resource_scope=[{"path": "/tmp/team-events", "access_mode": "read_write"}],
            scope_receipt=_task_receipt(),
            event_actor=f"lead:{conversation_id}",
            event_cause="crash-call",
        )

    assert await store.list_board_tasks(lead_conversation_id=conversation_id) == []
    after = await store.list_team_events(team_id=team.id)
    assert [(event.sequence, event.hash) for event in after] == [
        (event.sequence, event.hash) for event in before
    ]
    assert (await store.verify_team_event_log(team_id=team.id)).head_hash == before[-1].hash


async def test_team_budget_reserve_charge_settle_and_exhaustion_are_atomic(
    tmp_path: Path,
) -> None:
    store = SqliteCoworkStore(tmp_path / "cowork.db")
    await store.initialize()
    conversation_id = await store.create_conversation(title="team budget")
    team, workers = await store.create_team(
        lead_conversation_id=conversation_id,
        proposal_call_id="budget-proposal",
        note="",
        members=[
            {
                "name": "worker",
                "role": "consume bounded resources",
                "reason": "budget test",
                "state": {"status": "idle", "messages": []},
            }
        ],
        budget_limits={
            "model_calls": 5,
            "tool_calls": 4,
            "wall_ms": 30_000,
            "assignments": 1,
        },
    )
    task = await store.create_board_task(
        lead_conversation_id=conversation_id,
        title="bounded work",
        description="",
        acceptance_criteria="done",
        resource_scope=[],
    )
    started, _, session = await store.start_board_task(
        lead_conversation_id=conversation_id,
        task_id=task.id,
        worker_name=workers[0].name,
        assignment_call_id="budget-assignment-1",
        budget_reservation={"model_calls": 2, "tool_calls": 1, "wall_ms": 1_000},
    )
    reserved = await store.get_team_for_lead(lead_conversation_id=conversation_id)
    assert reserved is not None
    assert reserved.budget_usage == {
        "model_calls": 0,
        "tool_calls": 0,
        "wall_ms": 0,
        "assignments": 1,
        "reserved_model_calls": 2,
        "reserved_tool_calls": 1,
        "reserved_wall_ms": 1_000,
    }

    reservation = await store.charge_team_budget(
        session_id=session.id,
        task_id=started.id,
        dimension="model_calls",
        amount=1,
        event_actor=f"worker:{workers[0].id}",
        event_cause="budget-assignment-1",
    )
    assert reservation.used["model_calls"] == 1
    await store.charge_team_budget(
        session_id=session.id,
        task_id=started.id,
        dimension="wall_ms",
        amount=25,
        event_actor=f"worker:{workers[0].id}",
        event_cause="budget-assignment-1",
    )
    await store.complete_board_task(
        session_id=session.id,
        task_id=started.id,
        state={"status": "idle", "messages": []},
        worker_report="done",
    )
    settled = await store.get_team_for_lead(lead_conversation_id=conversation_id)
    assert settled is not None
    assert settled.budget_usage == {
        "model_calls": 1,
        "tool_calls": 0,
        "wall_ms": 25,
        "assignments": 1,
        "reserved_model_calls": 0,
        "reserved_tool_calls": 0,
        "reserved_wall_ms": 0,
    }

    second = await store.create_board_task(
        lead_conversation_id=conversation_id,
        title="must not start",
        description="",
        acceptance_criteria="assignment cap",
        resource_scope=[],
    )
    with pytest.raises(TeamBudgetExceededError) as caught:
        await store.start_board_task(
            lead_conversation_id=conversation_id,
            task_id=second.id,
            worker_name=workers[0].name,
            assignment_call_id="budget-assignment-2",
            budget_reservation={"model_calls": 1, "tool_calls": 0, "wall_ms": 1},
        )
    assert caught.value.dimension == "assignments"
    paused = await store.get_team_for_lead(lead_conversation_id=conversation_id)
    assert paused is not None
    assert (paused.status, paused.pause_reason) == ("paused", "budget:assignments")
    assert paused.budget_usage == settled.budget_usage
    tasks = await store.list_board_tasks(lead_conversation_id=conversation_id)
    assert next(item for item in tasks if item.id == second.id).status == "open"
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM cowork_team_budget_reservations WHERE task_id = ?",
                (str(second.id),),
            ).fetchone()[0]
            == 0
        )
    events = await store.list_team_events(team_id=team.id)
    assert events[-1].event_type == "team.budget_exceeded"
    assert events[-1].payload["dimension"] == "assignments"


async def test_worker_tool_attempt_ledger_retries_reads_and_fail_closes_writes(
    tmp_path: Path,
) -> None:
    store = SqliteCoworkStore(tmp_path / "cowork.db")
    await store.initialize()
    conversation_id, team, worker = await _create_team(store)
    task = await store.create_board_task(
        lead_conversation_id=conversation_id,
        title="attempt ledger",
        description="",
        acceptance_criteria="no duplicate effects",
        resource_scope=[{"path": "/tmp/team-events", "access_mode": "read_only"}],
    )
    started, _, session = await store.start_board_task(
        lead_conversation_id=conversation_id,
        task_id=task.id,
        worker_name=worker.name,
        assignment_call_id="attempt-assignment",
        budget_reservation={"model_calls": 2, "tool_calls": 4, "wall_ms": 1_000},
    )

    read_attempt = await store.begin_team_worker_tool_attempt(
        session_id=session.id,
        task_id=started.id,
        tool_call_id="read-call",
        tool_name="read_file",
        effect="none",
        retry_safe=True,
        arguments_sha256="read-arguments",
        event_actor=f"worker:{worker.id}",
        event_cause="attempt-assignment",
    )
    retried = await store.begin_team_worker_tool_attempt(
        session_id=session.id,
        task_id=started.id,
        tool_call_id="read-call",
        tool_name="read_file",
        effect="none",
        retry_safe=True,
        arguments_sha256="read-arguments",
        event_actor=f"worker:{worker.id}",
        event_cause="attempt-assignment",
    )
    assert (retried.id, retried.status, retried.attempt_count) == (
        read_attempt.id,
        "in_flight",
        2,
    )
    finished = await store.finish_team_worker_tool_attempt(
        attempt_id=retried.id,
        status="succeeded",
        result={"content": '{"ok":true}'},
        effect_ref="file:read",
        authorization_receipt={"mechanism": "path_scope"},
        event_actor=f"worker:{worker.id}",
        event_cause="attempt-assignment",
    )
    replayed = await store.begin_team_worker_tool_attempt(
        session_id=session.id,
        task_id=started.id,
        tool_call_id="read-call",
        tool_name="read_file",
        effect="none",
        retry_safe=True,
        arguments_sha256="read-arguments",
        event_actor=f"worker:{worker.id}",
        event_cause="attempt-assignment",
    )
    assert replayed == finished
    assert replayed.authorization_receipt == {"mechanism": "path_scope"}

    secret = "sk-ledger-secret-token-value"
    secret_attempt = await store.begin_team_worker_tool_attempt(
        session_id=session.id,
        task_id=started.id,
        tool_call_id="secret-read-call",
        tool_name="remote_read",
        effect="none",
        retry_safe=True,
        arguments_sha256="secret-read-arguments",
        event_actor=f"worker:{worker.id}",
        event_cause="attempt-assignment",
    )
    await store.finish_team_worker_tool_attempt(
        attempt_id=secret_attempt.id,
        status="succeeded",
        result={
            "content": f"Bearer {secret}",
            "headers": {"authorization": secret},
            "url": f"https://example.test/?token={secret}",
        },
        effect_ref=f"https://example.test/effect?token={secret}",
        authorization_receipt=None,
        event_actor=f"worker:{worker.id}",
        event_cause="attempt-assignment",
    )
    with sqlite3.connect(store.path) as connection:
        persisted_secret = connection.execute(
            "SELECT result, effect_ref FROM cowork_team_worker_tool_attempts WHERE id = ?",
            (str(secret_attempt.id),),
        ).fetchone()
    assert persisted_secret is not None
    assert secret not in " ".join(str(value) for value in persisted_secret)
    assert "<redacted>" in str(persisted_secret[0])

    oversized = await store.begin_team_worker_tool_attempt(
        session_id=session.id,
        task_id=started.id,
        tool_call_id="oversized-call",
        tool_name="remote_read",
        effect="none",
        retry_safe=True,
        arguments_sha256="oversized-arguments",
        event_actor=f"worker:{worker.id}",
        event_cause="attempt-assignment",
    )
    with pytest.raises(ValueError, match="result 超过持久化上限"):
        await store.finish_team_worker_tool_attempt(
            attempt_id=oversized.id,
            status="succeeded",
            result={"content": "x" * 30_000},
            effect_ref=None,
            authorization_receipt=None,
            event_actor=f"worker:{worker.id}",
            event_cause="attempt-assignment",
        )
    with sqlite3.connect(store.path) as connection:
        persisted_oversized = connection.execute(
            "SELECT status, result FROM cowork_team_worker_tool_attempts WHERE id = ?",
            (str(oversized.id),),
        ).fetchone()
    assert persisted_oversized == ("in_flight", None)
    with pytest.raises(ValueError, match="last_error"):
        await store.fail_board_task(
            session_id=session.id,
            task_id=started.id,
            state={"status": "failed"},
            error="s" * 501,
        )

    write_attempt = await store.begin_team_worker_tool_attempt(
        session_id=session.id,
        task_id=started.id,
        tool_call_id="write-call",
        tool_name="write_file",
        effect="filesystem",
        retry_safe=False,
        arguments_sha256="write-arguments",
        event_actor=f"worker:{worker.id}",
        event_cause="attempt-assignment",
    )
    unknown = await store.begin_team_worker_tool_attempt(
        session_id=session.id,
        task_id=started.id,
        tool_call_id="write-call",
        tool_name="write_file",
        effect="filesystem",
        retry_safe=False,
        arguments_sha256="write-arguments",
        event_actor=f"worker:{worker.id}",
        event_cause="attempt-assignment",
    )
    assert (unknown.id, unknown.status, unknown.attempt_count) == (
        write_attempt.id,
        "unknown",
        1,
    )
    with pytest.raises(ValueError, match="结果未知"):
        await store.finish_team_worker_tool_attempt(
            attempt_id=unknown.id,
            status="succeeded",
            result={"content": "must not settle"},
            effect_ref="file:unknown",
            authorization_receipt=None,
            event_actor=f"worker:{worker.id}",
            event_cause="attempt-assignment",
        )
    failed = await store.fail_board_task(
        session_id=session.id,
        task_id=started.id,
        state={"status": "failed"},
        error=f"Bearer {secret}",
        event_actor=f"worker:{worker.id}",
        event_cause="attempt-assignment",
    )
    assert secret not in (failed.last_error or "")
    event_types = [event.event_type for event in await store.list_team_events(team_id=team.id)]
    assert "team.worker_tool.retried" in event_types
    assert "team.worker_tool.finished" in event_types
    assert "team.worker_tool.unknown" in event_types
    assert event_types[-1] == "team.budget_settled"


async def test_team_lifecycle_stops_mutations_and_archive_fail_closes_active_work(
    tmp_path: Path,
) -> None:
    store = SqliteCoworkStore(tmp_path / "cowork.db")
    await store.initialize()
    conversation_id, team, worker = await _create_team(store, with_receipt=True)
    task = await store.create_board_task(
        lead_conversation_id=conversation_id,
        title="lifecycle",
        description="",
        acceptance_criteria="honor pause",
        resource_scope=[],
    )
    started, _, session = await store.start_board_task(
        lead_conversation_id=conversation_id,
        task_id=task.id,
        worker_name=worker.name,
        assignment_call_id="lifecycle-1",
        budget_reservation={"model_calls": 2, "tool_calls": 0, "wall_ms": 1_000},
    )

    with pytest.raises(ValueError, match="人工批准路径"):
        await store.manage_team(
            lead_conversation_id=conversation_id,
            action="pause",
            reason="agent tried to pause",
            event_actor=f"lead:{conversation_id}",
            event_cause="agent-manage-attempt",
        )
    paused = await store.manage_team(
        lead_conversation_id=conversation_id,
        action="pause",
        reason="user requested a stop",
        event_actor="human:user",
        event_cause="manage-pause",
    )
    assert (paused.status, paused.pause_reason) == ("paused", "user requested a stop")
    with pytest.raises(ValueError, match="安全点停止"):
        await store.validate_team_worker_execution(session_id=session.id, task_id=started.id)
    with pytest.raises(ValueError, match="不再允许 Worker checkpoint"):
        await store.save_team_worker_session(
            session_id=session.id,
            task_id=started.id,
            state={"status": "active"},
        )
    with pytest.raises(ValueError, match="active"):
        await store.create_board_task(
            lead_conversation_id=conversation_id,
            title="paused mutation",
            description="",
            acceptance_criteria="must fail",
            resource_scope=[],
        )

    resumed = await store.manage_team(
        lead_conversation_id=conversation_id,
        action="resume",
        budget_limits={
            "model_calls": 120,
            "tool_calls": 300,
            "wall_ms": 4_000_000,
            "assignments": 30,
        },
        reason="user reviewed the task",
        event_actor="human:user",
        event_cause="manage-resume",
    )
    assert resumed.status == "active"
    assert resumed.pause_reason is None
    assert resumed.budget_limits["assignments"] == 30
    await store.validate_team_worker_execution(session_id=session.id, task_id=started.id)
    await store.fail_board_task(
        session_id=session.id,
        task_id=started.id,
        state={"status": "failed"},
        error="test cleanup",
    )

    revoked = await store.manage_team(
        lead_conversation_id=conversation_id,
        action="revoke_write_delegation",
        reason="remove standing write authority",
        event_actor="human:user",
        event_cause="manage-revoke",
    )
    assert revoked.status == "active"
    assert revoked.write_delegation_scope == []
    assert revoked.write_delegation_receipt is None

    final_task = await store.create_board_task(
        lead_conversation_id=conversation_id,
        title="archive while running",
        description="",
        acceptance_criteria="blocked atomically",
        resource_scope=[],
    )
    running, _, final_session = await store.start_board_task(
        lead_conversation_id=conversation_id,
        task_id=final_task.id,
        worker_name=worker.name,
        assignment_call_id="lifecycle-2",
        budget_reservation={"model_calls": 2, "tool_calls": 0, "wall_ms": 1_000},
    )
    archived = await store.manage_team(
        lead_conversation_id=conversation_id,
        action="archive",
        reason="team is no longer needed",
        event_actor="human:user",
        event_cause="manage-archive",
    )
    assert (archived.status, archived.pause_reason) == ("archived", "team is no longer needed")
    rows = await store.list_board_tasks(lead_conversation_id=conversation_id)
    archived_task = next(item for item in rows if item.id == running.id)
    assert archived_task.status == "blocked"
    assert "Team archived" in (archived_task.last_error or "")
    with pytest.raises(ValueError, match="不再执行"):
        await store.validate_team_worker_execution(
            session_id=final_session.id,
            task_id=running.id,
        )
    with pytest.raises(ValueError, match="archived Team"):
        await store.manage_team(
            lead_conversation_id=conversation_id,
            action="resume",
            reason="must remain terminal",
            event_actor="human:user",
            event_cause="manage-invalid-resume",
        )

    events = await store.list_team_events(team_id=team.id)
    lifecycle_events = [
        event
        for event in events
        if event.event_type
        in {
            "team.paused",
            "team.resumed",
            "team.write_delegation_revoked",
            "team.archived",
        }
    ]
    assert [event.event_type for event in lifecycle_events] == [
        "team.paused",
        "team.resumed",
        "team.write_delegation_revoked",
        "team.archived",
    ]
    assert all(event.actor == "human:user" for event in lifecycle_events)


async def test_team_event_cursor_is_durable_hash_bound_and_gap_free(tmp_path: Path) -> None:
    path = tmp_path / "cowork.db"
    store = SqliteCoworkStore(path)
    await store.initialize()
    _, team, _ = await _create_team(store, with_receipt=True)
    events = await store.list_team_events(team_id=team.id)

    first = await store.advance_team_event_cursor(
        team_id=team.id,
        consumer="wake-driver",
        expected_sequence=0,
        event_sequence=1,
        event_hash=events[0].hash,
    )
    assert first.last_sequence == 1
    reopened = SqliteCoworkStore(path)
    await reopened.initialize()
    persisted = await reopened.get_team_event_cursor(team_id=team.id, consumer="wake-driver")
    assert persisted is not None
    assert (persisted.last_sequence, persisted.last_event_hash) == (1, events[0].hash)

    with pytest.raises(ValueError, match="无跳号"):
        await reopened.advance_team_event_cursor(
            team_id=team.id,
            consumer="wake-driver",
            expected_sequence=1,
            event_sequence=3,
            event_hash=events[-1].hash,
        )
    with pytest.raises(TeamEventIntegrityError, match="event/hash"):
        await reopened.advance_team_event_cursor(
            team_id=team.id,
            consumer="wake-driver",
            expected_sequence=1,
            event_sequence=2,
            event_hash="0" * 64,
        )
    second = await reopened.advance_team_event_cursor(
        team_id=team.id,
        consumer="wake-driver",
        expected_sequence=1,
        event_sequence=2,
        event_hash=events[1].hash,
    )
    assert second.last_sequence == 2


async def test_team_wake_outbox_is_allowlisted_recoverable_and_ack_advances_cursor(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cowork.db"
    store = SqliteCoworkStore(path)
    await store.initialize()
    conversation_id, team, worker = await _create_team(store)
    task = await store.create_board_task(
        lead_conversation_id=conversation_id,
        title="durable wake",
        description="",
        acceptance_criteria="delivered at least once",
        resource_scope=[],
    )
    started, _, session = await store.start_board_task(
        lead_conversation_id=conversation_id,
        task_id=task.id,
        worker_name=worker.name,
        assignment_call_id="wake-assignment",
        budget_reservation={"model_calls": 2, "tool_calls": 0, "wall_ms": 1_000},
    )

    delivered: list[tuple[UUID, str, str, str | None]] = []

    async def sink(delivery) -> str:
        delivered.append(
            (delivery.id, delivery.event_type, delivery.target_kind, delivery.target_id)
        )
        return f"durable-inbox:{delivery.id}"

    # team.created、task.created、budget_reserved 都在 durable feed，但不在 wake allowlist；
    # dispatcher 逐条 suppressed+ack，cursor 无跳号前进且 sink 不接到它们。
    for index in range(3):
        assert (
            await dispatch_team_wakes_once(
                sink=sink,
                claim_owner=f"drain-{index}",
                store=store,
                limit=10,
            )
            == 1
        )
    assert delivered == []

    first_claim = await store.claim_team_wake_deliveries(
        consumer=TEAM_WAKE_CONSUMER,
        claim_owner="worker-before-crash",
        limit=10,
        lease_seconds=30,
    )
    assert len(first_claim) == 1
    assignment = first_claim[0]
    assert (assignment.event_type, assignment.target_kind, assignment.target_id) == (
        "board.task.assigned",
        "worker",
        str(worker.id),
    )
    await store.manage_team(
        lead_conversation_id=conversation_id,
        action="pause",
        reason="verify dispatch safety gate",
        event_actor="human:user",
        event_cause="wake-pause",
    )
    with pytest.raises(ValueError, match="非 active"):
        await store.validate_team_wake_delivery(
            delivery_id=assignment.id,
            claim_owner="worker-before-crash",
        )
    await store.release_team_wake_delivery(
        delivery_id=assignment.id,
        claim_owner="worker-before-crash",
        error="expected paused Team gate",
    )
    await store.manage_team(
        lead_conversation_id=conversation_id,
        action="resume",
        reason="continue dispatch test",
        event_actor="human:user",
        event_cause="wake-resume",
    )
    resumed_claim = await store.claim_team_wake_deliveries(
        consumer=TEAM_WAKE_CONSUMER,
        claim_owner="worker-before-crash",
        limit=10,
        lease_seconds=30,
    )
    assert len(resumed_claim) == 1 and resumed_claim[0].id == assignment.id
    assignment = resumed_claim[0]
    assert (
        await store.validate_team_wake_delivery(
            delivery_id=assignment.id,
            claim_owner="worker-before-crash",
        )
        == "deliver"
    )
    first_receipt = await sink(assignment)
    cursor_before_ack = await store.get_team_event_cursor(
        team_id=team.id,
        consumer=TEAM_WAKE_CONSUMER,
    )
    assert cursor_before_ack is not None
    assert cursor_before_ack.last_sequence == assignment.event_sequence - 1

    # 模拟 sink 已持久收件、进程却在 ack 前退出。lease 过期后重领的是同一 delivery id；
    # 下游用它幂等，ack 和 cursor CAS 在同一个 SQLite 事务完成。
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE cowork_team_wake_outbox SET claim_until = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", str(assignment.id)),
        )
    reopened = SqliteCoworkStore(path)
    await reopened.initialize()
    reclaimed = await reopened.claim_team_wake_deliveries(
        consumer=TEAM_WAKE_CONSUMER,
        claim_owner="worker-after-crash",
        limit=10,
        lease_seconds=30,
    )
    assert len(reclaimed) == 1
    assert (reclaimed[0].id, reclaimed[0].attempt_count) == (assignment.id, 3)
    assert (
        await reopened.validate_team_wake_delivery(
            delivery_id=assignment.id,
            claim_owner="worker-after-crash",
        )
        == "deliver"
    )
    acknowledged = await reopened.ack_team_wake_delivery(
        delivery_id=assignment.id,
        consumer=TEAM_WAKE_CONSUMER,
        claim_owner="worker-after-crash",
        delivery_receipt=first_receipt,
    )
    assert (acknowledged.status, acknowledged.delivery_receipt) == (
        "delivered",
        first_receipt,
    )
    cursor_after_ack = await reopened.get_team_event_cursor(
        team_id=team.id,
        consumer=TEAM_WAKE_CONSUMER,
    )
    assert cursor_after_ack is not None
    assert cursor_after_ack.last_sequence == assignment.event_sequence

    # pause/resume 是持久 feed，但不在固定 wake allowlist。
    for index in range(2):
        assert (
            await dispatch_team_wakes_once(
                sink=sink,
                claim_owner=f"lifecycle-drain-{index}",
                store=reopened,
            )
            == 1
        )

    await reopened.complete_board_task(
        session_id=session.id,
        task_id=started.id,
        state={"status": "idle", "messages": []},
        worker_report="ready for Lead",
    )
    assert (
        await dispatch_team_wakes_once(
            sink=sink,
            claim_owner="lead-delivery",
            store=reopened,
        )
        == 1
    )
    assert delivered[-1][1:] == (
        "board.task.submitted",
        "lead",
        str(conversation_id),
    )

    with sqlite3.connect(path) as connection:
        targets = connection.execute(
            """SELECT event_type, target_kind FROM cowork_team_wake_outbox
               WHERE target_kind <> 'none' ORDER BY event_sequence"""
        ).fetchall()
    assert targets == [
        ("board.task.assigned", "worker"),
        ("board.task.submitted", "lead"),
    ]


@pytest.mark.parametrize(
    ("action", "with_receipt", "resource_scope"),
    [
        ("archive", False, []),
        (
            "revoke_write_delegation",
            True,
            [{"path": "/tmp/team-events", "access_mode": "read_write"}],
        ),
    ],
)
async def test_terminal_team_wake_is_suppressed_without_blocking_later_feed(
    tmp_path: Path,
    action: str,
    with_receipt: bool,
    resource_scope: list[dict[str, str]],
) -> None:
    store = SqliteCoworkStore(tmp_path / action / "cowork.db")
    await store.initialize()
    conversation_id, team, worker = await _create_team(store, with_receipt=with_receipt)
    task = await store.create_board_task(
        lead_conversation_id=conversation_id,
        title="terminal wake",
        description="",
        acceptance_criteria="old wake must not revive",
        resource_scope=resource_scope,
        scope_receipt=_task_receipt() if with_receipt else None,
    )
    started, _, _ = await store.start_board_task(
        lead_conversation_id=conversation_id,
        task_id=task.id,
        worker_name=worker.name,
        assignment_call_id="terminal-wake-assignment",
        budget_reservation={"model_calls": 2, "tool_calls": 1, "wall_ms": 1_000},
    )

    assignment = None
    while assignment is None:
        claimed = await store.claim_team_wake_deliveries(
            consumer=TEAM_WAKE_CONSUMER,
            claim_owner="terminal-drain",
            limit=10,
            lease_seconds=30,
        )
        assert len(claimed) == 1
        current = claimed[0]
        if current.event_type == "board.task.assigned":
            assignment = current
            break
        assert (
            await store.validate_team_wake_delivery(
                delivery_id=current.id,
                claim_owner="terminal-drain",
            )
            == "suppress"
        )
        await store.ack_team_wake_delivery(
            delivery_id=current.id,
            consumer=TEAM_WAKE_CONSUMER,
            claim_owner="terminal-drain",
            delivery_receipt=f"suppressed:{current.event_type}",
        )

    await store.manage_team(
        lead_conversation_id=conversation_id,
        action=action,  # type: ignore[arg-type]
        reason="terminal authorization decision",
        event_actor="human:user",
        event_cause=f"terminal-{action}",
    )
    current_task = next(
        item
        for item in await store.list_board_tasks(lead_conversation_id=conversation_id)
        if item.id == started.id
    )
    assert current_task.status == "blocked"
    assert (
        await store.validate_team_wake_delivery(
            delivery_id=assignment.id,
            claim_owner="terminal-drain",
        )
        == "suppress"
    )
    await store.ack_team_wake_delivery(
        delivery_id=assignment.id,
        consumer=TEAM_WAKE_CONSUMER,
        claim_owner="terminal-drain",
        delivery_receipt="suppressed:terminal-team",
    )

    delivered: list[str] = []

    async def sink(delivery) -> str:
        delivered.append(delivery.event_type)
        return f"lead-inbox:{delivery.id}"

    for index in range(8):
        await dispatch_team_wakes_once(
            sink=sink,
            claim_owner=f"terminal-followup-{index}",
            store=store,
        )
        if "board.task.blocked" in delivered:
            break
    if action == "archive":
        assert delivered == []
    else:
        assert "board.task.blocked" in delivered
    cursor = await store.get_team_event_cursor(
        team_id=team.id,
        consumer=TEAM_WAKE_CONSUMER,
    )
    assert cursor is not None and cursor.last_sequence > assignment.event_sequence


async def test_lead_wake_source_ids_are_idempotent_after_delivery_before_ack(
    tmp_path: Path,
) -> None:
    store = SqliteCoworkStore(tmp_path / "cowork.db")
    await store.initialize()
    conversation_id = await store.create_conversation(title="wake sink idempotence")
    run_wake_id = uuid7()
    first_run = await store.create_run(
        conversation_id=conversation_id,
        goal="durable lead wake",
        budget_tokens=1_000,
        budget_calls=2,
        budget_wall_ms=1_000,
        source_wake_id=run_wake_id,
    )
    retried_run = await store.create_run(
        conversation_id=conversation_id,
        goal="must return existing",
        budget_tokens=9_999,
        budget_calls=9,
        budget_wall_ms=9_999,
        source_wake_id=run_wake_id,
    )
    assert retried_run.id == first_run.id

    steering_wake_id = uuid7()
    first_steering = await store.enqueue_steering(
        run_id=first_run.id,
        conversation_id=conversation_id,
        content="lead wake",
        source_wake_id=steering_wake_id,
    )
    retried_steering = await store.enqueue_steering(
        run_id=first_run.id,
        conversation_id=conversation_id,
        content="duplicate must not be inserted",
        source_wake_id=steering_wake_id,
    )
    assert retried_steering.id == first_steering.id
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE source_wake_id = ?",
                (str(run_wake_id),),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM cowork_steering_messages WHERE source_wake_id = ?",
                (str(steering_wake_id),),
            ).fetchone()[0]
            == 1
        )


async def test_explicit_conversation_delete_purges_team_audit_payloads(tmp_path: Path) -> None:
    path = tmp_path / "cowork.db"
    store = SqliteCoworkStore(path)
    await store.initialize()
    conversation_id, _, worker = await _create_team(store)
    task = await store.create_board_task(
        lead_conversation_id=conversation_id,
        title="private task title",
        description="private task description",
        acceptance_criteria="private acceptance criteria",
        resource_scope=[],
    )
    await store.start_board_task(
        lead_conversation_id=conversation_id,
        task_id=task.id,
        worker_name=worker.name,
        assignment_call_id="privacy-delete-assignment",
    )

    assert await store.delete_conversation(conversation_id=conversation_id)
    with sqlite3.connect(path) as connection:
        counts = dict(
            connection.execute(
                """SELECT 'teams', COUNT(*) FROM cowork_teams
                   UNION ALL SELECT 'workers', COUNT(*) FROM cowork_team_workers
                   UNION ALL SELECT 'sessions', COUNT(*) FROM cowork_team_worker_sessions
                   UNION ALL SELECT 'tasks', COUNT(*) FROM cowork_board_tasks
                   UNION ALL SELECT 'budgets', COUNT(*) FROM cowork_team_budget_reservations
                   UNION ALL SELECT 'attempts', COUNT(*) FROM cowork_team_worker_tool_attempts
                   UNION ALL SELECT 'events', COUNT(*) FROM cowork_team_events
                   UNION ALL SELECT 'heads', COUNT(*) FROM cowork_team_event_heads
                   UNION ALL SELECT 'cursors', COUNT(*) FROM cowork_team_event_cursors
                   UNION ALL SELECT 'projections', COUNT(*)
                       FROM cowork_team_event_projection_summaries
                   UNION ALL SELECT 'outbox', COUNT(*) FROM cowork_team_wake_outbox
                   UNION ALL SELECT 'purge_guards', COUNT(*)
                       FROM cowork_team_event_purge_guards"""
            ).fetchall()
        )
    assert set(counts.values()) == {0}
