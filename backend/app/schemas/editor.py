from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class EditableDocumentResponse(BaseModel):
    document_id: UUID
    title: str
    source_name: str
    source_uri: str
    content: str
    baseline_sha256: str
    version_no: int | None
    updated_at_ns: int


class EditProposalRequest(BaseModel):
    baseline_sha256: str = Field(min_length=64, max_length=64)
    content: str = Field(max_length=500_000)
    instruction: str = Field(min_length=1, max_length=4_000)
    selection_start: int = Field(ge=0)
    selection_end: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_selection(self) -> "EditProposalRequest":
        if self.selection_end < self.selection_start:
            raise ValueError("selection_end 不能小于 selection_start")
        if self.selection_end > len(self.content):
            raise ValueError("选区超出文档内容范围")
        return self


class EditProposalResponse(BaseModel):
    instruction: str
    selection_start: int
    selection_end: int
    original_text: str
    replacement_text: str
    proposed_content: str
    baseline_sha256: str
    model: str
    provider: str


class ApplyDocumentRequest(BaseModel):
    baseline_sha256: str = Field(min_length=64, max_length=64)
    content: str = Field(max_length=500_000)


class ApplyDocumentResponse(BaseModel):
    document_id: UUID
    title: str
    source_uri: str
    content: str
    baseline_sha256: str
    version_id: UUID | None
    version_no: int | None
    indexed: bool
    index_error: str | None


class EditorPermissionResponse(BaseModel):
    granted: bool
    scope: str
    expires_in_s: int


class ExecuteDocumentResponse(BaseModel):
    proposal: EditProposalResponse
    document: ApplyDocumentResponse


WorkspaceFileKind = Literal["markdown", "word", "excel"]


class WorkspaceFileSummary(BaseModel):
    file_id: str
    name: str
    source_name: str
    source_uri: str
    kind: WorkspaceFileKind
    size_bytes: int
    updated_at_ns: int


class WorkspaceFileListResponse(BaseModel):
    items: list[WorkspaceFileSummary]


class WorkspaceFileResponse(WorkspaceFileSummary):
    content: str
    baseline_sha256: str
    editable: bool = True


class WorkspaceInstructionRequest(BaseModel):
    baseline_sha256: str = Field(min_length=64, max_length=64)
    instruction: str = Field(min_length=1, max_length=4_000)
    # Markdown 允许把前端尚未保存的手工修改一起送入；Word/Excel 忽略此字段，
    # 始终从磁盘重新读取结构，避免浏览器伪造二进制文档状态。
    content: str | None = Field(default=None, max_length=500_000)
    selection_start: int = Field(default=0, ge=0)
    selection_end: int = Field(default=0, ge=0)


class WorkspaceInstructionResponse(BaseModel):
    file: WorkspaceFileResponse
    summary: str
    change_count: int
    model: str
    provider: str
    backup_uri: str | None
