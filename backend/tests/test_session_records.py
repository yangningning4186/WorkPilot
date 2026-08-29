from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.agent_core.session_records import (
    SessionRecord,
    SessionRecordCorruptionError,
    reduce_model_step_attempts,
    reduce_session_records,
)

_RUN_ID = uuid4()


def _record(
    *,
    seq: int,
    operation_id: str,
    phase: str,
    attempt_no: int = 1,
    payload: dict[str, object] | None = None,
) -> SessionRecord:
    identity: dict[str, object] = {
        "source_checkpoint_id": "checkpoint-1",
        "result_checkpoint_id": "checkpoint-2",
        "iteration": 0,
        "attempt_no": attempt_no,
    }
    if payload is not None:
        identity.update(payload)
    return SessionRecord(
        id=f"record-{seq}",
        run_id=_RUN_ID,
        seq=seq,
        kind="step_attempt",
        operation_id=operation_id,
        phase=phase,  # type: ignore[arg-type]
        payload=identity,
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
    )


def _intent_record(
    *,
    seq: int,
    kind: str,
    operation_id: str,
    phase: str,
    payload: dict[str, object],
) -> SessionRecord:
    return SessionRecord(
        id=f"intent-{seq}",
        run_id=_RUN_ID,
        seq=seq,
        kind=kind,  # type: ignore[arg-type]
        operation_id=operation_id,
        phase=phase,  # type: ignore[arg-type]
        payload=payload,
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
    )


def test_model_step_attempt_reducer_preserves_an_open_intent() -> None:
    attempts = reduce_model_step_attempts(
        [_record(seq=1, operation_id="model-turn-1", phase="started")]
    )

    assert len(attempts) == 1
    assert attempts[0].phase == "started"
    assert attempts[0].result is None


def test_model_step_attempt_reducer_replaces_start_with_terminal_result() -> None:
    attempts = reduce_model_step_attempts(
        [
            _record(seq=1, operation_id="model-turn-1", phase="started"),
            _record(
                seq=2,
                operation_id="model-turn-1",
                phase="completed",
                payload={"result": {"stop_reason": "complete"}},
            ),
        ]
    )

    assert attempts[0].phase == "completed"
    assert attempts[0].result == {"stop_reason": "complete"}


def test_model_step_attempt_reducer_separates_sidecar_steps_and_ignores_queue_records() -> None:
    queue_record = SessionRecord(
        id="queue-record",
        run_id=_RUN_ID,
        seq=3,
        kind="queue_event",
        operation_id="queue:message-1",
        phase="enqueued",
        payload={"message_id": "message-1"},
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
    )
    attempts = reduce_model_step_attempts(
        [
            _record(seq=1, operation_id="assistant-1", phase="started"),
            _record(
                seq=2,
                operation_id="compaction-1",
                phase="started",
                payload={"step": "compaction"},
            ),
            queue_record,
        ]
    )

    assert [(attempt.step, attempt.attempt_no) for attempt in attempts] == [
        ("assistant", 1),
        ("compaction", 1),
    ]


def test_session_record_reducer_projects_queue_abort_and_open_action_intents() -> None:
    queue_identity = {
        "message_id": "message-1",
        "requested_delivery": "follow_up",
        "delivery": "next_run",
        "source": "local_owner",
    }
    reduced = reduce_session_records(
        [
            _intent_record(
                seq=1,
                kind="queue_event",
                operation_id="queue:message-1",
                phase="enqueued",
                payload=queue_identity,
            ),
            _intent_record(
                seq=2,
                kind="queue_event",
                operation_id="queue:message-1",
                phase="consumed",
                payload={**queue_identity, "launched_run_id": "run-2"},
            ),
            _intent_record(
                seq=3,
                kind="abort_requested",
                operation_id=f"abort:{_RUN_ID}",
                phase="requested",
                payload={"source": "control_plane"},
            ),
            _intent_record(
                seq=4,
                kind="harness_action",
                operation_id="action:prepare-1",
                phase="started",
                payload={"action": "prepare", "iteration": 2},
            ),
        ]
    )

    assert reduced.queue_events[0].phase == "consumed"
    assert reduced.queue_events[0].launched_run_id == "run-2"
    assert reduced.abort_request is not None
    assert reduced.abort_request.source == "control_plane"
    assert reduced.harness_actions[0].phase == "started"
    assert reduced.harness_actions[0].action == "prepare"


@pytest.mark.parametrize(
    ("records", "reason"),
    [
        (
            [
                _intent_record(
                    seq=1,
                    kind="queue_event",
                    operation_id="queue:message-1",
                    phase="consumed",
                    payload={
                        "message_id": "message-1",
                        "requested_delivery": "steer",
                        "delivery": "steer",
                        "source": "local_owner",
                    },
                )
            ],
            "queue_terminal_without_enqueue",
        ),
        (
            [
                _intent_record(
                    seq=1,
                    kind="harness_action",
                    operation_id="action:dispatch-1",
                    phase="completed",
                    payload={"action": "dispatch", "iteration": 0},
                )
            ],
            "action_terminal_without_start",
        ),
        (
            [
                _intent_record(
                    seq=1,
                    kind="abort_requested",
                    operation_id="abort:not-this-run",
                    phase="requested",
                    payload={"source": "control_plane"},
                )
            ],
            "invalid_abort_request",
        ),
        (
            [
                _intent_record(
                    seq=1,
                    kind="harness_action",
                    operation_id="tool-action:orphan",
                    phase="started",
                    payload={
                        "action": "tool",
                        "tool_call_id": "call-1",
                        "tool_name": "read_file",
                        "index": 0,
                    },
                )
            ],
            "open_tool_without_execute_action",
        ),
    ],
)
def test_session_record_reducer_rejects_corrupt_non_model_intents(
    records: list[SessionRecord],
    reason: str,
) -> None:
    with pytest.raises(SessionRecordCorruptionError) as caught:
        reduce_session_records(records)

    assert caught.value.reason == reason


@pytest.mark.parametrize(
    ("records", "reason"),
    [
        (
            [_record(seq=1, operation_id="model-turn-2", phase="started", attempt_no=2)],
            "non_consecutive_attempt",
        ),
        (
            [
                _record(
                    seq=1,
                    operation_id="model-turn-1",
                    phase="completed",
                    payload={"result": {"stop_reason": "complete"}},
                )
            ],
            "terminal_without_start",
        ),
        (
            [
                _record(seq=1, operation_id="model-turn-1", phase="started"),
                _record(
                    seq=2,
                    operation_id="model-turn-2",
                    phase="started",
                    attempt_no=2,
                ),
            ],
            "multiple_open_attempts",
        ),
        (
            [
                _record(seq=1, operation_id="model-turn-1", phase="started"),
                _record(
                    seq=2,
                    operation_id="model-turn-1",
                    phase="completed",
                    payload={"result": {"stop_reason": "complete"}},
                ),
                _record(
                    seq=3,
                    operation_id="model-turn-2",
                    phase="started",
                    attempt_no=2,
                ),
            ],
            "record_after_finish",
        ),
        (
            [
                _record(seq=1, operation_id="model-turn-1", phase="started"),
                _record(
                    seq=2,
                    operation_id="model-turn-1",
                    phase="failed",
                    payload={
                        "result_checkpoint_id": "different-checkpoint",
                        "result": {},
                    },
                ),
            ],
            "attempt_identity_mismatch",
        ),
    ],
)
def test_model_step_attempt_corruption_has_stable_reason(
    records: list[SessionRecord],
    reason: str,
) -> None:
    with pytest.raises(SessionRecordCorruptionError) as caught:
        reduce_model_step_attempts(records)

    assert caught.value.reason == reason
