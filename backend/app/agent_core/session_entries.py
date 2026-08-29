"""会话树 entry 契约与确定性损坏分类。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

SessionEntryKind = Literal[
    "message",
    "model_change",
    "thinking_level_change",
    "active_tools_change",
    "compaction",
    "branch_summary",
    "custom",
]
SessionCorruptionReason = Literal[
    "duplicate_entry",
    "dangling_parent",
    "parent_cycle",
    "non_monotonic_sequence",
    "lane_head_missing",
    "entry_after_terminal",
    "invalid_payload",
    "invalid_kind",
    "conversation_mismatch",
    "multiple_roots",
    "duplicate_message_reference",
    "branch_position_mismatch",
]


class SessionCorruptionError(RuntimeError):
    def __init__(self, reason: SessionCorruptionReason, detail: str) -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True)
class SessionEntry:
    id: str
    conversation_id: UUID
    parent_id: str | None
    seq: int
    kind: SessionEntryKind
    payload: dict[str, Any]
    created_at: str


def validate_session_tree(
    entries: list[SessionEntry],
    *,
    lane_heads: dict[str, str | None] | None = None,
) -> None:
    """纯函数校验；发现矛盾即具名拒绝，不猜测修复。"""

    valid_kinds = {
        "message",
        "model_change",
        "thinking_level_change",
        "active_tools_change",
        "compaction",
        "branch_summary",
        "custom",
    }
    by_id: dict[str, SessionEntry] = {}
    conversation_id: UUID | None = None
    for entry in entries:
        if entry.id in by_id:
            raise SessionCorruptionError("duplicate_entry", entry.id)
        if not isinstance(entry.payload, dict):
            raise SessionCorruptionError("invalid_payload", entry.id)
        if entry.kind not in valid_kinds:
            raise SessionCorruptionError("invalid_kind", f"{entry.id}={entry.kind!r}")
        if conversation_id is None:
            conversation_id = entry.conversation_id
        elif entry.conversation_id != conversation_id:
            raise SessionCorruptionError("conversation_mismatch", entry.id)
        by_id[entry.id] = entry

    for entry in entries:
        if entry.parent_id is not None and entry.parent_id not in by_id:
            raise SessionCorruptionError("dangling_parent", entry.id)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(entry_id: str) -> None:
        if entry_id in visiting:
            raise SessionCorruptionError("parent_cycle", entry_id)
        if entry_id in visited:
            return
        visiting.add(entry_id)
        parent_id = by_id[entry_id].parent_id
        if parent_id is not None:
            visit(parent_id)
        visiting.remove(entry_id)
        visited.add(entry_id)

    for entry_id in by_id:
        visit(entry_id)

    message_refs: set[str] = set()
    terminal_paths: set[str] = set()
    roots = 0
    previous_seq = -1
    for entry in sorted(entries, key=lambda item: item.seq):
        if entry.seq <= previous_seq:
            raise SessionCorruptionError("non_monotonic_sequence", entry.id)
        previous_seq = entry.seq
        if entry.parent_id is None:
            roots += 1
        else:
            parent = by_id[entry.parent_id]
            if parent.seq >= entry.seq:
                raise SessionCorruptionError("branch_position_mismatch", entry.id)
            if parent.id in terminal_paths:
                raise SessionCorruptionError("entry_after_terminal", entry.id)
        if entry.kind == "message":
            record_id = entry.payload.get("record_id")
            if not isinstance(record_id, str) or not record_id:
                raise SessionCorruptionError("invalid_payload", entry.id)
            if record_id in message_refs:
                raise SessionCorruptionError("duplicate_message_reference", record_id)
            message_refs.add(record_id)
        if entry.payload.get("terminal") is True or (
            entry.parent_id is not None and entry.parent_id in terminal_paths
        ):
            terminal_paths.add(entry.id)
    if len(entries) > 0 and roots != 1:
        raise SessionCorruptionError("multiple_roots", f"roots={roots}")
    for lane, head in (lane_heads or {}).items():
        if head is not None and head not in by_id:
            raise SessionCorruptionError("lane_head_missing", f"{lane}={head}")
