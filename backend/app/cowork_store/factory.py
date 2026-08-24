"""Cowork 本地 store 生命周期；PostgreSQL RAG session 由 app.core.db 独立管理。"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.cowork_store.jsonl import JsonlConversationStore
from app.cowork_store.sqlite import SqliteCoworkStore


@dataclass(frozen=True)
class LocalCoworkStores:
    state: SqliteCoworkStore
    conversations: JsonlConversationStore


_local_stores: LocalCoworkStores | None = None


async def initialize_local_cowork_stores(settings: Settings) -> LocalCoworkStores:
    global _local_stores
    if _local_stores is None:
        root = settings.cowork_data_path.expanduser().resolve()
        stores = LocalCoworkStores(
            state=SqliteCoworkStore(root / "cowork.db"),
            conversations=JsonlConversationStore(root / "conversations"),
        )
        await stores.state.initialize()
        await stores.conversations.initialize()
        _local_stores = stores
    return _local_stores


def local_cowork_stores() -> LocalCoworkStores:
    if _local_stores is None:
        raise RuntimeError("Cowork 本地 store 尚未初始化")
    return _local_stores


async def close_local_cowork_stores() -> None:
    global _local_stores
    if _local_stores is not None:
        await _local_stores.state.close()
        _local_stores = None
