from typing import Literal

from pydantic import BaseModel

WorkspaceFileKind = Literal["markdown", "word", "excel"]


class WorkspaceFileSummary(BaseModel):
    file_id: str
    name: str
    source_name: str
    source_uri: str
    kind: WorkspaceFileKind
    size_bytes: int
    updated_at_ns: int


class WorkspaceFileResponse(WorkspaceFileSummary):
    content: str
    baseline_sha256: str
    editable: bool = True


class WorkspaceInstructionResponse(BaseModel):
    file: WorkspaceFileResponse
    summary: str
    change_count: int
    model: str
    provider: str
    backup_uri: str | None
