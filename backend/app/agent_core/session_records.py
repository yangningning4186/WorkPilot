"""Harness intent records that are durable but never sent to the model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

SessionRecordKind = Literal[
    "step_attempt",
    "queue_event",
    "abort_requested",
    "harness_action",
]
SessionRecordPhase = Literal[
    "started",
    "completed",
    "failed",
    "enqueued",
    "consumed",
    "cancelled",
    "requested",
]
ModelStepKind = Literal["assistant", "compaction", "forced_final"]


@dataclass(frozen=True)
class SessionRecord:
    id: str
    run_id: UUID
    seq: int
    kind: SessionRecordKind
    operation_id: str
    phase: SessionRecordPhase
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class ModelStepAttemptState:
    operation_id: str
    source_checkpoint_id: str
    result_checkpoint_id: str
    iteration: int
    attempt_no: int
    step: ModelStepKind
    phase: SessionRecordPhase
    result: dict[str, Any] | None


@dataclass(frozen=True)
class QueueEventState:
    operation_id: str
    message_id: str
    requested_delivery: Literal["steer", "follow_up", "next_run"]
    delivery: Literal["steer", "follow_up", "next_run"]
    source: str
    phase: Literal["enqueued", "consumed", "cancelled"]
    launched_run_id: str | None = None


@dataclass(frozen=True)
class AbortRequestState:
    operation_id: str
    source: str


@dataclass(frozen=True)
class HarnessActionState:
    operation_id: str
    action: Literal["prepare", "dispatch", "materialize", "execute", "tool"]
    phase: Literal["started", "completed", "failed"]
    iteration: int | None
    tool_call_id: str | None = None
    tool_name: str | None = None
    index: int | None = None


@dataclass(frozen=True)
class ReducedSessionRecords:
    """Pure projection of every durable harness intent for one run."""

    model_attempts: tuple[ModelStepAttemptState, ...]
    queue_events: tuple[QueueEventState, ...]
    abort_request: AbortRequestState | None
    harness_actions: tuple[HarnessActionState, ...]


class SessionRecordCorruptionError(ValueError):
    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}")


class ModelInvocationOutcomeUnknownError(RuntimeError):
    """A dispatched model attempt has no durable terminal record."""


def reduce_model_step_attempts(
    records: list[SessionRecord],
) -> list[ModelStepAttemptState]:
    """Reduce the append-only attempt log and reject contradictory histories."""

    seen_record_ids: set[str] = set()
    seen_sequences: set[int] = set()
    run_id: UUID | None = None
    states: dict[str, ModelStepAttemptState] = {}
    next_attempt: dict[tuple[str, int, str], int] = {}
    latest_by_step: dict[tuple[str, int, str], ModelStepAttemptState] = {}
    ordered: list[ModelStepAttemptState] = []
    for record in sorted(records, key=lambda item: item.seq):
        if run_id is None:
            run_id = record.run_id
        elif record.run_id != run_id:
            raise SessionRecordCorruptionError("mixed_run_records", str(record.run_id))
        if record.id in seen_record_ids:
            raise SessionRecordCorruptionError("duplicate_record", record.id)
        if record.seq in seen_sequences:
            raise SessionRecordCorruptionError("duplicate_sequence", str(record.seq))
        if record.seq < 1:
            raise SessionRecordCorruptionError("invalid_sequence", str(record.seq))
        seen_record_ids.add(record.id)
        seen_sequences.add(record.seq)
        if record.kind in {"queue_event", "abort_requested", "harness_action"}:
            continue
        if record.kind != "step_attempt":
            raise SessionRecordCorruptionError("unknown_record_kind", str(record.kind))
        if record.phase not in {"started", "completed", "failed"}:
            raise SessionRecordCorruptionError(
                "invalid_step_attempt_phase", f"{record.operation_id}/{record.phase}"
            )
        payload = record.payload
        source = payload.get("source_checkpoint_id")
        result_checkpoint = payload.get("result_checkpoint_id")
        iteration = payload.get("iteration")
        attempt_no = payload.get("attempt_no")
        step = payload.get("step", "assistant")
        if (
            not isinstance(source, str)
            or not source
            or not isinstance(result_checkpoint, str)
            or not result_checkpoint
            or not isinstance(iteration, int)
            or iteration < 0
            or not isinstance(attempt_no, int)
            or attempt_no < 1
            or step not in {"assistant", "compaction", "forced_final"}
        ):
            raise SessionRecordCorruptionError("invalid_step_attempt_payload", record.operation_id)
        key = (source, iteration, step)
        if record.phase == "started":
            if record.operation_id in states:
                raise SessionRecordCorruptionError("duplicate_attempt_start", record.operation_id)
            latest = latest_by_step.get(key)
            if latest is not None:
                if latest.phase == "started":
                    raise SessionRecordCorruptionError(
                        "multiple_open_attempts", f"{latest.operation_id}/{record.operation_id}"
                    )
                latest_reason = None if latest.result is None else latest.result.get("stop_reason")
                if latest.phase == "completed" or latest_reason != "retryable_error":
                    raise SessionRecordCorruptionError("record_after_finish", record.operation_id)
            expected = next_attempt.get(key, 1)
            if attempt_no != expected:
                raise SessionRecordCorruptionError(
                    "non_consecutive_attempt",
                    f"{source}/{iteration}: expected {expected}, got {attempt_no}",
                )
            next_attempt[key] = expected + 1
            state = ModelStepAttemptState(
                operation_id=record.operation_id,
                source_checkpoint_id=source,
                result_checkpoint_id=result_checkpoint,
                iteration=iteration,
                attempt_no=attempt_no,
                step=cast("ModelStepKind", step),
                phase="started",
                result=None,
            )
            states[record.operation_id] = state
            latest_by_step[key] = state
            ordered.append(state)
            continue
        previous = states.get(record.operation_id)
        if previous is None:
            raise SessionRecordCorruptionError("terminal_without_start", record.operation_id)
        if previous.phase != "started":
            raise SessionRecordCorruptionError("duplicate_attempt_terminal", record.operation_id)
        if (
            previous.source_checkpoint_id != source
            or previous.result_checkpoint_id != result_checkpoint
            or previous.iteration != iteration
            or previous.attempt_no != attempt_no
            or previous.step != step
        ):
            raise SessionRecordCorruptionError("attempt_identity_mismatch", record.operation_id)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise SessionRecordCorruptionError("invalid_attempt_result", record.operation_id)
        stop_reason = result.get("stop_reason")
        if record.phase == "completed" and stop_reason not in {"complete", "truncated"}:
            raise SessionRecordCorruptionError("terminal_phase_mismatch", record.operation_id)
        if record.phase == "failed" and stop_reason in {"complete", "truncated"}:
            raise SessionRecordCorruptionError("terminal_phase_mismatch", record.operation_id)
        terminal = ModelStepAttemptState(
            operation_id=record.operation_id,
            source_checkpoint_id=source,
            result_checkpoint_id=result_checkpoint,
            iteration=iteration,
            attempt_no=attempt_no,
            step=previous.step,
            phase=record.phase,
            result=result,
        )
        states[record.operation_id] = terminal
        latest_by_step[key] = terminal
        ordered[ordered.index(previous)] = terminal
    return ordered


def reduce_session_records(records: list[SessionRecord]) -> ReducedSessionRecords:
    """Reduce all record kinds and reject intent histories that cannot be replayed safely.

    The model-attempt reducer remains available as a compatibility projection, but recovery
    should use this function so a corrupt queue/action log cannot be silently ignored merely
    because the next paid model call has not started yet.
    """

    model_attempts = tuple(reduce_model_step_attempts(records))
    queue_by_operation: dict[str, QueueEventState] = {}
    queue_order: list[str] = []
    actions_by_operation: dict[str, HarnessActionState] = {}
    action_order: list[str] = []
    abort_request: AbortRequestState | None = None
    deliveries = {"steer", "follow_up", "next_run"}
    action_kinds = {"prepare", "dispatch", "materialize", "execute", "tool"}

    for record in sorted(records, key=lambda item: item.seq):
        if record.kind == "step_attempt":
            continue
        payload = record.payload
        if record.kind == "queue_event":
            message_id = payload.get("message_id")
            requested = payload.get("requested_delivery")
            delivery = payload.get("delivery")
            source = payload.get("source")
            if (
                not isinstance(message_id, str)
                or not message_id
                or record.operation_id != f"queue:{message_id}"
                or requested not in deliveries
                or delivery not in deliveries
                or not isinstance(source, str)
                or not source
                or record.phase not in {"enqueued", "consumed", "cancelled"}
            ):
                raise SessionRecordCorruptionError(
                    "invalid_queue_event_payload", record.operation_id
                )
            previous = queue_by_operation.get(record.operation_id)
            if record.phase == "enqueued":
                if previous is not None:
                    raise SessionRecordCorruptionError(
                        "duplicate_queue_enqueue", record.operation_id
                    )
                queue_order.append(record.operation_id)
            elif previous is None:
                raise SessionRecordCorruptionError(
                    "queue_terminal_without_enqueue", record.operation_id
                )
            elif previous.phase != "enqueued":
                raise SessionRecordCorruptionError("duplicate_queue_terminal", record.operation_id)
            elif (
                previous.message_id != message_id
                or previous.requested_delivery != requested
                or previous.delivery != delivery
                or previous.source != source
            ):
                raise SessionRecordCorruptionError("queue_identity_mismatch", record.operation_id)
            launched_run_id = payload.get("launched_run_id")
            if launched_run_id is not None and (
                record.phase != "consumed"
                or delivery != "next_run"
                or not isinstance(launched_run_id, str)
                or not launched_run_id
            ):
                raise SessionRecordCorruptionError("invalid_queue_launch", record.operation_id)
            queue_by_operation[record.operation_id] = QueueEventState(
                operation_id=record.operation_id,
                message_id=message_id,
                requested_delivery=cast("Literal['steer', 'follow_up', 'next_run']", requested),
                delivery=cast("Literal['steer', 'follow_up', 'next_run']", delivery),
                source=source,
                phase=cast("Literal['enqueued', 'consumed', 'cancelled']", record.phase),
                launched_run_id=launched_run_id,
            )
            continue
        if record.kind == "abort_requested":
            source = payload.get("source")
            if (
                record.phase != "requested"
                or record.operation_id != f"abort:{record.run_id}"
                or not isinstance(source, str)
                or not source
            ):
                raise SessionRecordCorruptionError("invalid_abort_request", record.operation_id)
            if abort_request is not None:
                raise SessionRecordCorruptionError("duplicate_abort_request", record.operation_id)
            abort_request = AbortRequestState(operation_id=record.operation_id, source=source)
            continue
        if record.kind == "harness_action":
            action = payload.get("action")
            iteration = payload.get("iteration")
            if (
                action not in action_kinds
                or record.phase not in {"started", "completed", "failed"}
                or (
                    iteration is not None
                    and (
                        not isinstance(iteration, int)
                        or isinstance(iteration, bool)
                        or iteration < 0
                    )
                )
            ):
                raise SessionRecordCorruptionError(
                    "invalid_harness_action_payload", record.operation_id
                )
            tool_call_id = payload.get("tool_call_id")
            tool_name = payload.get("tool_name")
            index = payload.get("index")
            if action == "tool":
                if (
                    not isinstance(tool_call_id, str)
                    or not tool_call_id
                    or not isinstance(tool_name, str)
                    or not tool_name
                    or not isinstance(index, int)
                    or isinstance(index, bool)
                    or index < 0
                ):
                    raise SessionRecordCorruptionError(
                        "invalid_tool_action_payload", record.operation_id
                    )
            elif any(value is not None for value in (tool_call_id, tool_name, index)):
                raise SessionRecordCorruptionError(
                    "unexpected_tool_action_fields", record.operation_id
                )
            previous_action = actions_by_operation.get(record.operation_id)
            if record.phase == "started":
                if previous_action is not None:
                    raise SessionRecordCorruptionError(
                        "duplicate_action_start", record.operation_id
                    )
                action_order.append(record.operation_id)
            elif previous_action is None:
                raise SessionRecordCorruptionError(
                    "action_terminal_without_start", record.operation_id
                )
            elif previous_action.phase != "started":
                raise SessionRecordCorruptionError("duplicate_action_terminal", record.operation_id)
            elif (
                previous_action.action != action
                or previous_action.iteration != iteration
                or previous_action.tool_call_id != tool_call_id
                or previous_action.tool_name != tool_name
                or previous_action.index != index
            ):
                raise SessionRecordCorruptionError("action_identity_mismatch", record.operation_id)
            actions_by_operation[record.operation_id] = HarnessActionState(
                operation_id=record.operation_id,
                action=cast(
                    "Literal['prepare', 'dispatch', 'materialize', 'execute', 'tool']", action
                ),
                phase=cast("Literal['started', 'completed', 'failed']", record.phase),
                iteration=iteration,
                tool_call_id=cast("str | None", tool_call_id),
                tool_name=cast("str | None", tool_name),
                index=cast("int | None", index),
            )
            continue
        raise SessionRecordCorruptionError("unknown_record_kind", str(record.kind))

    open_top_level = [
        action
        for action in actions_by_operation.values()
        if action.phase == "started" and action.action != "tool"
    ]
    if len(open_top_level) > 1:
        raise SessionRecordCorruptionError(
            "multiple_open_harness_actions",
            "/".join(action.operation_id for action in open_top_level),
        )
    open_tools = [
        action
        for action in actions_by_operation.values()
        if action.phase == "started" and action.action == "tool"
    ]
    if open_tools and (not open_top_level or open_top_level[0].action != "execute"):
        raise SessionRecordCorruptionError(
            "open_tool_without_execute_action",
            "/".join(action.operation_id for action in open_tools),
        )

    return ReducedSessionRecords(
        model_attempts=model_attempts,
        queue_events=tuple(queue_by_operation[item] for item in queue_order),
        abort_request=abort_request,
        harness_actions=tuple(actions_by_operation[item] for item in action_order),
    )


__all__ = [
    "AbortRequestState",
    "HarnessActionState",
    "ModelInvocationOutcomeUnknownError",
    "ModelStepAttemptState",
    "ModelStepKind",
    "QueueEventState",
    "ReducedSessionRecords",
    "SessionRecord",
    "SessionRecordCorruptionError",
    "SessionRecordKind",
    "SessionRecordPhase",
    "reduce_model_step_attempts",
    "reduce_session_records",
]
