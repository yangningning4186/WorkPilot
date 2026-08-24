"""Checkpoint 的稳定契约；具体 SQLite/PostgreSQL 读写由 adapter 实现。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class StateCheckpoint[StateT]:
    checkpoint_id: str
    state: StateT


class CheckpointStore(Protocol):
    async def save_checkpoint(
        self,
        *,
        run_id: UUID,
        checkpoint_id: str,
        parent_id: str | None,
        state: dict[str, Any],
    ) -> None: ...

    async def load_latest_checkpoint(
        self,
        *,
        run_id: UUID,
    ) -> Any | None: ...
