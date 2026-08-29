from __future__ import annotations

from typing import get_args, is_typeddict

import pytest

from app.run_events import RUN_EVENT_TYPES, RunEventInput, RunEventType, run_event


def test_run_event_input_is_a_closed_payload_discriminated_union() -> None:
    arms = get_args(RunEventInput)
    discriminators: list[str] = []
    for arm in arms:
        event_type, payload_type = get_args(arm)
        literal_values = get_args(event_type)
        assert len(literal_values) == 1
        assert is_typeddict(payload_type)
        discriminators.append(literal_values[0])

    assert set(discriminators) == set(get_args(RunEventType)) == RUN_EVENT_TYPES
    assert len(discriminators) == len(set(discriminators))


def test_dynamic_run_event_boundary_rejects_unknown_names() -> None:
    assert run_event("message.delta", {"text": "hello"}) == (
        "message.delta",
        {"text": "hello"},
    )
    with pytest.raises(ValueError, match="未知 RunEvent type"):
        run_event("message.typo", {"text": "hello"})
