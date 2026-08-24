from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field


class LocalDirCreateRequest(BaseModel):
    root: Path = Path(".")
    name: str | None = Field(default=None, min_length=1, max_length=200)


class LocalDirSourceResponse(BaseModel):
    id: UUID
    name: str
    root: Path
    sync_status: str
    sync_error: str | None
    document_count: int


class LocalDirSyncRequest(BaseModel):
    max_chunk_chars: int = Field(default=2000, ge=200, le=20000)


class SyncFailureResponse(BaseModel):
    source_uri: str
    error: str


class LocalDirSyncResponse(BaseModel):
    source_id: UUID
    added: int
    updated: int
    skipped: int
    deleted: int
    failed: int
    failures: list[SyncFailureResponse]
