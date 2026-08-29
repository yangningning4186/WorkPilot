from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    "network.fetch",
    "browser.read",
    "browser.write",
    "browser.destructive",
    "sandbox.execute",
    "host.execute",
    "external.read",
    "external.write",
    "external.destructive",
]
GrantableCapability = Literal[
    "knowledge.read",
    "filesystem.read",
    "filesystem.write",
    "network.fetch",
    "browser.read",
    "browser.write",
    "browser.destructive",
    "sandbox.execute",
    "host.execute",
    "external.read",
    "external.write",
    "external.destructive",
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
    capability: GrantableCapability
    session_root_id: UUID | None = None
    resource_scope: str | None = Field(default=None, min_length=1, max_length=2048)
    expires_in_s: int | None = Field(default=None, ge=300, le=30 * 24 * 60 * 60)

    @model_validator(mode="after")
    def validate_scope(self) -> "CapabilityGrantCreate":
        path_capability = self.capability.startswith("filesystem.")
        if path_capability != (self.session_root_id is not None):
            raise ValueError("文件能力必须绑定目录，网络/Shell/外部能力不能绑定目录")
        scoped = self.capability == "network.fetch"
        if scoped != (self.resource_scope is not None):
            raise ValueError("network.fetch 必须绑定 origin/domain scope，其他能力不能带网络 scope")
        return self


class CapabilityGrantResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    session_root_id: UUID | None
    capability: Capability
    resource_scope: str | None
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
    match_kind: Literal[
        "action_target",
        "argv_pattern",
        "tool",
        "target",
        "command_prefix",
    ]
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


class MemoryUndo(BaseModel):
    previous_memory_id: UUID


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


class ArtifactDiffResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    available: bool
    format: Literal["unified"] = "unified"
    view: Literal["text", "semantic", "unavailable"]
    created: bool = False
    before_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    after_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    added_lines: int = Field(default=0, ge=0)
    removed_lines: int = Field(default=0, ge=0)
    truncated: bool = False
    text: str = Field(default="", max_length=50_000)
    reason: str | None = Field(default=None, max_length=500)


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


class ReadingAnnotationResponse(BaseModel):
    id: UUID
    locator: int
    quote: str
    note: str
    color: Literal["yellow", "green", "blue", "pink"]
    # 约束 3 的完整几何，原样来自解析结果。空列表表示这条批注只锚在 locator 上。
    locations: list[dict[str, Any]]
    created_at: datetime


class ReadingAnnotationCreate(BaseModel):
    """用户自己划出来的批注。

    与模型的 `reader_annotate` 走**同一张表、同一套几何来源**，只有引文对不上时的处理
    不同：模型被拒绝，用户被降级成"只记不画"。理由是引文的来源不一样——模型给的
    quote 可能是它自己的翻译或复述，拒绝挡的正是一个凭空捏造的锚点；用户的 quote 是
    从文本层里逐字划下来的，对不上说明是我们的归一化没跟上 PDF 的硬换行/连字，
    这时候拒绝等于拿自己的短板去驳用户。
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    locator: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=2_000)
    # 备注可以为空：划一段高亮本身就是一条信息，逼用户写点什么才肯存下来是没道理的。
    note: str = Field(default="", max_length=2_000)
    color: Literal["yellow", "green", "blue", "pink"] = "yellow"


class ReadingAnnotationCreated(BaseModel):
    annotation: ReadingAnnotationResponse
    # 引文有没有逐字命中。false 表示这条批注只锚在 locator 上、画不出框——界面据此
    # 说清楚"记下了，但没能定位到具体位置"，而不是让用户对着一条不出现的高亮发呆。
    verified: bool


class ReadingAnnotationsResponse(BaseModel):
    material_id: str
    items: list[ReadingAnnotationResponse]
    # 这个路径上属于**别的**内容版本的批注条数。它们不显示（几何可能已经指向别的
    # 文字），但也不能静默消失——改了一版 PDF 之后批注全没了却查不到为什么，
    # 是最糟的那种失败。
    stale_count: int


class KnowledgeBaseVersionResponse(BaseModel):
    """一版索引。同一批文档上可以并存多版，界面据此做切换与 A/B。"""

    version_id: str
    label: str
    embedding: str
    engine: Literal["hybrid", "dense", "bm25"]
    retrieval: str
    node_count: int
    # 这一版建出来之后 KB 又加过文档：仍然可用，只是覆盖不到新的那几篇。不自动重建，
    # 否则"加一篇文档"会变成几分钟的全量作业，还会悄悄改掉评测已经引用过的版本。
    stale: bool
    is_active: bool


class KnowledgeBaseDocumentResponse(BaseModel):
    doc_id: str
    filename: str
    title: str
    parser: str
    char_count: int
    content_hash: str
    # 相对 KB 目录，避免把内部 data root 暴露给客户端；空表示旧库尚待首次安全迁移。
    snapshot_path: str | None
    snapshot_available: bool


class KnowledgeBaseResponse(BaseModel):
    slug: str
    name: str
    description: str
    document_count: int
    # 没建过索引 / 建到一半失败的库都会是 False。挂载允许，但检索会给出"请重建"的错误——
    # 让用户在列表里先看见这个状态，比在回答里看见一句检索失败要早得多。
    is_indexed: bool
    # active 那一版用哪个 embedding 建的。换模型之后这一行会和当前配置对不上，
    # 检索随即拒绝服务（约束 10）。
    embedding: str | None
    active_version: str | None
    versions: list[KnowledgeBaseVersionResponse]
    # 旧的单索引布局，还没迁到版本化布局。检索会显式拒绝并指向 rebuild——不做静默兼容，
    # 因为那意味着给这一版编一组它其实没用过的检索配置，A/B 从此不可比。
    needs_migration: bool
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


class KnowledgeBaseVersionCreate(BaseModel):
    """在现有文档集合上构建一版索引；建好前旧版本始终可用。"""

    version_id: str | None = Field(default=None, min_length=1, max_length=63)
    label: str = Field(default="", max_length=64)
    engine: Literal["hybrid", "dense", "bm25"] = "hybrid"
    activate: bool = True


class KnowledgeBaseAddDocuments(BaseModel):
    """按本机路径导入。目录会递归展开成里面支持的格式。

    走路径而不是浏览器上传：这是本机桌面应用。服务会在导入时把原始字节复制进
    WorkPilot 的内容寻址快照目录，之后重建不再依赖这个外部路径。
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
