from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, JsonValue, model_validator

EvalCategory = Literal[
    "single_hop",
    "multi_hop",
    "table",
    "temporal",
    "unanswerable",
    "global",
    "agent_task",
]
EvalOrigin = Literal["human", "synthetic", "badcase"]
EvalSplit = Literal["dev", "test", "regression"]


class AnnotationDatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    split: EvalSplit = "dev"
    version: str = Field(default="1", min_length=1, max_length=50)
    description: str | None = Field(default=None, max_length=1000)


class AnnotationDatasetResponse(BaseModel):
    id: UUID
    name: str
    split: EvalSplit
    version: str
    description: str | None
    item_count: int
    valid_count: int
    stale_count: int


class AnnotationDocumentResponse(BaseModel):
    document_id: UUID
    version_id: UUID
    title: str
    source_uri: str
    parser: str
    parser_version: str
    page_count: int | None
    block_count: int
    source_kind: str


class AnnotationLocationResponse(BaseModel):
    page_no: int
    page_width: float
    page_height: float
    rotation: int
    coord_origin: str
    bbox_norm: list[float]


class AnnotationBlockResponse(BaseModel):
    block_id: UUID
    version_id: UUID
    block_idx: int
    block_type: str
    text: str
    char_start: int
    char_end: int
    heading_path: list[str]
    locations: list[AnnotationLocationResponse]


class AnnotationBlockPageResponse(BaseModel):
    items: list[AnnotationBlockResponse]
    total: int
    offset: int
    limit: int


class ResolveSpanRequest(BaseModel):
    block_id: UUID
    utf16_start: int = Field(ge=0)
    utf16_end: int = Field(gt=0)
    quote: str = Field(min_length=1)
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_offsets(self) -> "ResolveSpanRequest":
        if self.utf16_end <= self.utf16_start:
            raise ValueError("选区结束位置必须大于开始位置")
        return self


class GoldSpanResponse(BaseModel):
    version_id: UUID
    block_id: UUID
    char_start: int
    char_end: int
    quote: str
    note: str | None
    title: str
    source_uri: str
    locations: list[AnnotationLocationResponse]


class GoldSpanInput(BaseModel):
    version_id: UUID
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    quote: str = Field(min_length=1)
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_offsets(self) -> "GoldSpanInput":
        if self.char_end <= self.char_start:
            raise ValueError("gold span 结束位置必须大于开始位置")
        return self


class GoldToolInput(BaseModel):
    """agent_task 的期望工具调用。

    顺序即 gold 序列；arguments 只写必须匹配的参数子集，空对象表示只校验工具名。
    """

    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.:-]+$")
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class AnnotationItemUpsert(BaseModel):
    dataset_id: UUID
    category: EvalCategory
    question: str = Field(min_length=1, max_length=4000)
    gold_answer: str | None = Field(default=None, max_length=20000)
    gold_spans: list[GoldSpanInput] = Field(default_factory=list, max_length=20)
    gold_tools: list[GoldToolInput] = Field(default_factory=list, max_length=20)
    must_include: list[str] = Field(default_factory=list, max_length=30)
    must_not_include: list[str] = Field(default_factory=list, max_length=30)
    difficulty: int = Field(default=1, ge=1, le=3)
    origin: EvalOrigin = "human"
    temporal_ctx: datetime | None = None

    @model_validator(mode="after")
    def validate_answerability(self) -> "AnnotationItemUpsert":
        if self.category == "unanswerable":
            if self.gold_spans:
                raise ValueError("unanswerable 样本不能包含 gold span")
            if self.gold_answer and self.gold_answer.strip():
                raise ValueError("unanswerable 样本不能包含 gold answer")
            if self.gold_tools:
                raise ValueError("unanswerable 样本不能包含 gold tools")
        elif self.category == "agent_task":
            if not self.gold_tools:
                raise ValueError("agent_task 样本至少需要一个 gold tool")
        else:
            if self.gold_tools:
                raise ValueError("只有 agent_task 样本可以包含 gold tools")
            if self.category == "global":
                if not self.gold_answer or not self.gold_answer.strip():
                    raise ValueError("global 样本需要 gold answer")
            else:
                if not self.gold_spans:
                    raise ValueError("可答检索样本至少需要一个 gold span")
                if not self.gold_answer or not self.gold_answer.strip():
                    raise ValueError("可答检索样本需要 gold answer")
        if self.category == "temporal":
            if self.temporal_ctx is None:
                raise ValueError("temporal 样本需要 temporal_ctx")
        elif self.temporal_ctx is not None:
            raise ValueError("只有 temporal 样本可以设置 temporal_ctx")
        return self


class AnnotationItemResponse(BaseModel):
    id: UUID
    dataset_id: UUID
    category: EvalCategory
    question: str
    gold_answer: str | None
    gold_spans: list[GoldSpanInput]
    gold_tools: list[GoldToolInput]
    must_include: list[str]
    must_not_include: list[str]
    difficulty: int
    origin: EvalOrigin
    temporal_ctx: datetime | None
    status: Literal["valid", "stale", "invalid"]
    issues: list[str]
    updated_at: datetime


class AnnotationItemListResponse(BaseModel):
    items: list[AnnotationItemResponse]
    total: int
