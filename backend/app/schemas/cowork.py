from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

AccessMode = Literal["read_only", "read_write"]
Capability = Literal[
    "knowledge.read",
    "filesystem.read",
    "filesystem.write",
    "office.word.edit",
    "office.excel.edit",
    "network.read",
    "browser.control",
    "shell.execute",
    "external.action",
]


class SessionRootCreate(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    access_mode: AccessMode
    label: str | None = Field(default=None, min_length=1, max_length=200)


class SessionRootResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    requested_path: str
    canonical_path: str
    label: str
    access_mode: AccessMode
    enabled: bool
    created_at: datetime
    updated_at: datetime


class SessionRootListResponse(BaseModel):
    items: list[SessionRootResponse]


class CapabilityGrantCreate(BaseModel):
    capability: Capability
    session_root_id: UUID | None = None
    expires_in_s: int | None = Field(default=None, ge=300, le=30 * 24 * 60 * 60)

    @model_validator(mode="after")
    def validate_scope(self) -> "CapabilityGrantCreate":
        path_capability = self.capability.startswith(("filesystem.", "office."))
        if path_capability != (self.session_root_id is not None):
            raise ValueError("文件能力必须绑定目录，网络/Shell/外部能力不能绑定目录")
        return self


class CapabilityGrantResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    session_root_id: UUID | None
    capability: Capability
    grant_source: str
    expires_at: datetime | None
    revoked_at: datetime | None
    active: bool
    created_at: datetime
    updated_at: datetime


class CapabilityGrantListResponse(BaseModel):
    items: list[CapabilityGrantResponse]


class ApprovalRuleResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    scope: Literal["conversation", "schedule"]
    schedule_id: UUID | None
    tool: str
    match_kind: Literal["tool", "target", "command_prefix"]
    target: str | None
    created_by: str
    revoked_at: datetime | None
    active: bool
    created_at: datetime


class ApprovalRuleListResponse(BaseModel):
    items: list[ApprovalRuleResponse]


class WorkspaceTrustEntry(BaseModel):
    canonical_path: str
    trusted: bool
    # 仓库当前声明了哪些命令前缀，以及哪些条目被拒绝了。被静默丢掉的条目表现出来就是
    # "我明明写了它却还在弹审批"，所以必须回给界面。
    declared: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    config_error: str | None = None


class WorkspaceTrustListResponse(BaseModel):
    items: list[WorkspaceTrustEntry]


class WorkspaceTrustUpdate(BaseModel):
    canonical_path: str = Field(min_length=1, max_length=4096)
    trusted: bool


MemoryScope = Literal["global", "workspace", "conversation"]


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    scope: MemoryScope = "global"
    key: str | None = Field(default=None, min_length=1, max_length=120)


class MemoryPatch(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    # 撤销一次 forget。和 content 可以同时给：撤销一次"改写"就是还原旧文本。
    restore: bool = False

    @model_validator(mode="after")
    def _requires_a_change(self) -> "MemoryPatch":
        if self.content is None and not self.restore:
            raise ValueError("请提供要写入的内容，或设置 restore=true 恢复已 retire 的记忆")
        return self


class MemoryResponse(BaseModel):
    id: UUID
    scope: MemoryScope
    conversation_id: UUID | None
    workspace_path: str | None
    key: str | None
    content: str
    source: Literal["agent", "user"]
    created_at: datetime
    updated_at: datetime
    forgotten_at: datetime | None


class MemoryListResponse(BaseModel):
    items: list[MemoryResponse]


class ArtifactResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    run_id: UUID | None
    session_root_id: UUID | None
    kind: Literal["file", "report", "diff", "table"]
    title: str
    uri: str
    mime_type: str | None
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ArtifactListResponse(BaseModel):
    items: list[ArtifactResponse]


class AttachmentResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    message_id: UUID | None
    run_id: UUID | None
    kind: Literal["image", "pdf", "text"]
    filename: str
    media_type: str
    size_bytes: int
    sha256: str


class ReadingOutlineEntry(BaseModel):
    locator: int
    title: str
    level: int
    # True 表示这一行是用每个 unit 的首行凑的，不是文档自带的章节结构。前端据此提示
    # 用户"这只是线索"，不然一份没有书签的 PDF 会显示出一份看起来很正经的假目录。
    synthesised: bool


class ReadingMaterialResponse(BaseModel):
    path: str
    material_id: str
    filename: str
    title: str
    unit: Literal["page", "section"]
    unit_count: int
    parser: str
    # 只有 PDF 能忠实地渲染原页；其余格式阅读器显示抽取出来的文本。
    has_page_image: bool
    outline: list[ReadingOutlineEntry]


class ReadingUnitResponse(BaseModel):
    locator: int
    unit: Literal["page", "section"]
    text: str


class KnowledgeBaseDocumentResponse(BaseModel):
    doc_id: str
    filename: str
    title: str
    parser: str
    char_count: int


class KnowledgeBaseResponse(BaseModel):
    slug: str
    name: str
    description: str
    document_count: int
    # 没建过索引 / 建到一半失败的库都会是 False。挂载允许，但检索会给出"请重建"的错误——
    # 让用户在列表里先看见这个状态，比在回答里看见一句检索失败要早得多。
    is_indexed: bool
    # 用哪个 embedding 建的。换模型之后这一行会和当前配置对不上，检索随即拒绝服务。
    embedding: str | None
    documents: list[KnowledgeBaseDocumentResponse]


class KnowledgeBaseListResponse(BaseModel):
    items: list[KnowledgeBaseResponse]


class ConversationKnowledgeBase(BaseModel):
    """会话挂载了哪个 KB。`slug=None` 表示没挂 / 卸载。"""

    slug: str | None = Field(default=None, max_length=63)


class KnowledgeBaseCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=63)
    name: str = Field(default="", max_length=64)
    description: str = Field(default="", max_length=500)


class KnowledgeBaseAddDocuments(BaseModel):
    """按本机路径导入。目录会递归展开成里面支持的格式。

    走路径而不是上传：这是本机桌面应用，资料本来就在磁盘上。上传意味着把它们再复制一份进
    WorkPilot 的目录，占双份空间，而且清单里"按源路径重建"这条就断了。
    """

    paths: list[str] = Field(min_length=1, max_length=50)


class KnowledgeBaseSkipped(BaseModel):
    filename: str
    reason: str


class KnowledgeBaseIndexingJob(BaseModel):
    slug: str
    status: Literal["running", "done", "failed"]
    # 正在做什么，直接展示：「解析 attention.pdf」「建立索引」。
    stage: str
    done: int
    total: int
    added: int
    error: str | None
    skipped: list[KnowledgeBaseSkipped]
