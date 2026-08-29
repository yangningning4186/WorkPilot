from uuid import uuid4

import pytest

from app.agent_core.session_entries import (
    SessionCorruptionError,
    SessionEntry,
    validate_session_tree,
)


def _entry(
    entry_id: str,
    *,
    seq: int,
    parent_id: str | None,
    kind: str = "custom",
    payload: dict[str, object] | None = None,
    conversation_id=None,
) -> SessionEntry:
    return SessionEntry(
        id=entry_id,
        conversation_id=conversation_id or uuid4(),
        parent_id=parent_id,
        seq=seq,
        kind=kind,  # type: ignore[arg-type]
        payload=payload or {},
        created_at="2026-08-29T00:00:00+00:00",
    )


@pytest.mark.parametrize(
    ("entries", "reason"),
    [
        (
            lambda conversation: [
                _entry("root", seq=1, parent_id=None, conversation_id=conversation),
                _entry("child", seq=2, parent_id="missing", conversation_id=conversation),
            ],
            "dangling_parent",
        ),
        (
            lambda conversation: [
                _entry("a", seq=1, parent_id="b", conversation_id=conversation),
                _entry("b", seq=2, parent_id="a", conversation_id=conversation),
            ],
            "parent_cycle",
        ),
        (
            lambda conversation: [
                _entry(
                    "terminal",
                    seq=1,
                    parent_id=None,
                    payload={"terminal": True},
                    conversation_id=conversation,
                ),
                _entry("late", seq=2, parent_id="terminal", conversation_id=conversation),
            ],
            "entry_after_terminal",
        ),
        (
            lambda conversation: [
                _entry(
                    "message-1",
                    seq=1,
                    parent_id=None,
                    kind="message",
                    payload={"record_id": "same"},
                    conversation_id=conversation,
                ),
                _entry(
                    "message-2",
                    seq=2,
                    parent_id="message-1",
                    kind="message",
                    payload={"record_id": "same"},
                    conversation_id=conversation,
                ),
            ],
            "duplicate_message_reference",
        ),
    ],
)
def test_session_tree_corruption_has_stable_reason(entries, reason: str) -> None:
    conversation_id = uuid4()

    with pytest.raises(SessionCorruptionError) as caught:
        validate_session_tree(entries(conversation_id))

    assert caught.value.reason == reason


def test_session_tree_accepts_two_lanes_with_one_root() -> None:
    conversation_id = uuid4()
    entries = [
        _entry("root", seq=1, parent_id=None, conversation_id=conversation_id),
        _entry("main", seq=2, parent_id="root", conversation_id=conversation_id),
        _entry("branch", seq=3, parent_id="root", conversation_id=conversation_id),
    ]

    validate_session_tree(entries, lane_heads={"main": "main", "branch": "branch"})
