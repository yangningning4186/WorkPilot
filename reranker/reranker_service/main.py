import math
import os
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Protocol

import torch
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from transformers import AutoModelForSequenceClassification, AutoTokenizer


@dataclass(frozen=True)
class ServiceSettings:
    model: str
    revision: str
    device: str
    dtype: str
    batch_size: int
    max_length: int

    @classmethod
    def from_env(cls) -> "ServiceSettings":
        batch_size = int(os.getenv("RERANKER_BATCH_SIZE", "4"))
        max_length = int(os.getenv("RERANKER_MAX_LENGTH", "512"))
        if not 1 <= batch_size <= 64:
            raise ValueError("RERANKER_BATCH_SIZE 必须位于 1 到 64")
        if not 128 <= max_length <= 8192:
            raise ValueError("RERANKER_MAX_LENGTH 必须位于 128 到 8192")
        return cls(
            model=os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"),
            revision=os.getenv("RERANKER_REVISION", "main"),
            device=os.getenv("RERANKER_DEVICE", "auto"),
            dtype=os.getenv("RERANKER_DTYPE", "auto"),
            batch_size=batch_size,
            max_length=max_length,
        )


class AuditSpan(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> "AuditSpan":
        if self.char_end <= self.char_start:
            raise ValueError("audit span 结束位置必须大于开始位置")
        return self


class Document(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=20_000)
    audit_spans: list[AuditSpan] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_audit_spans(self) -> "Document":
        ids = [span.id for span in self.audit_spans]
        if len(ids) != len(set(ids)):
            raise ValueError("同一 document 的 audit span id 不能重复")
        if any(span.char_end > len(self.text) for span in self.audit_spans):
            raise ValueError("audit span 不能超过 document text 长度")
        return self


class RerankRequest(BaseModel):
    model: str | None = None
    query: str = Field(min_length=1, max_length=4_000)
    documents: list[Document] = Field(min_length=1, max_length=100)
    top_n: int = Field(default=10, ge=1, le=100)
    # 仅允许在已加载服务的安全上限内缩小窗口。生产请求不传时行为逐位不变;
    # 离线 2x2 实验可在同一模型实例上比较 512/1024, 避免重启引入候选漂移。
    max_length: int | None = Field(default=None, ge=128, le=8192)

    @model_validator(mode="after")
    def validate_request(self) -> "RerankRequest":
        if self.top_n > len(self.documents):
            raise ValueError("top_n 不能大于 documents 数量")
        ids = [document.id for document in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("document id 不能重复")
        return self


class RerankItem(BaseModel):
    index: int
    id: str
    relevance_score: float


class TokenSpanAudit(BaseModel):
    document_id: str
    span_id: str
    char_start: int
    char_end: int
    total_tokens: int
    visible_tokens: int
    fully_visible: bool


class RerankResponse(BaseModel):
    model: str
    results: list[RerankItem]
    span_audits: list[TokenSpanAudit] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    model: str
    revision: str
    device: str
    dtype: str
    batch_size: int
    max_length: int


class Scorer(Protocol):
    model_name: str
    revision: str
    device: str
    dtype: str
    batch_size: int
    max_length: int

    def score(
        self, query: str, documents: list[str], *, max_length: int | None = None
    ) -> list[float]: ...

    def audit_spans(
        self,
        query: str,
        documents: list[Document],
        *,
        max_length: int,
    ) -> list[TokenSpanAudit]: ...


class CrossEncoderScorer:
    def __init__(self, settings: ServiceSettings) -> None:
        self.model_name = settings.model
        self.revision = settings.revision
        self.device = _resolve_device(settings.device)
        self.dtype, torch_dtype = _resolve_dtype(settings.dtype, device=self.device)
        self.batch_size = settings.batch_size
        self.max_length = settings.max_length
        self._tokenizer = AutoTokenizer.from_pretrained(
            settings.model,
            revision=settings.revision,
        )
        self._model = AutoModelForSequenceClassification.from_pretrained(
            settings.model,
            revision=settings.revision,
            dtype=torch_dtype,
        )
        self._model.to(self.device)
        self._model.eval()

    def score(
        self, query: str, documents: list[str], *, max_length: int | None = None
    ) -> list[float]:
        effective_max_length = max_length or self.max_length
        scores: list[float] = []
        with torch.inference_mode():
            for offset in range(0, len(documents), self.batch_size):
                batch = documents[offset : offset + self.batch_size]
                inputs = self._tokenizer(
                    [query] * len(batch),
                    batch,
                    padding=True,
                    truncation=True,
                    max_length=effective_max_length,
                    return_tensors="pt",
                )
                inputs = {key: value.to(self.device) for key, value in inputs.items()}
                logits = self._model(**inputs, return_dict=True).logits.reshape(-1)
                scores.extend(torch.sigmoid(logits).float().cpu().tolist())
        if len(scores) != len(documents) or any(not math.isfinite(score) for score in scores):
            raise RuntimeError("reranker 返回了非法分数")
        return scores

    def audit_spans(
        self,
        query: str,
        documents: list[Document],
        *,
        max_length: int,
    ) -> list[TokenSpanAudit]:
        audits: list[TokenSpanAudit] = []
        for document in documents:
            if not document.audit_spans:
                continue
            complete = self._tokenizer(
                query,
                document.text,
                truncation=False,
                return_offsets_mapping=True,
            )
            visible = self._tokenizer(
                query,
                document.text,
                truncation=True,
                max_length=max_length,
                return_offsets_mapping=True,
            )
            complete_offsets = _document_offsets(complete)
            visible_offsets = _document_offsets(visible)
            for span in document.audit_spans:
                total_tokens, visible_tokens, fully_visible = _token_span_coverage(
                    complete_offsets,
                    visible_offsets,
                    char_start=span.char_start,
                    char_end=span.char_end,
                )
                audits.append(
                    TokenSpanAudit(
                        document_id=document.id,
                        span_id=span.id,
                        char_start=span.char_start,
                        char_end=span.char_end,
                        total_tokens=total_tokens,
                        visible_tokens=visible_tokens,
                        fully_visible=fully_visible,
                    )
                )
        return audits


def _document_offsets(encoding: object) -> list[tuple[int, int]]:
    """取 pair tokenizer 中第二段 document 的字符 offset。"""
    sequence_ids = encoding.sequence_ids()  # type: ignore[attr-defined]
    offsets = encoding["offset_mapping"]  # type: ignore[index]
    return [
        (int(start), int(end))
        for sequence_id, (start, end) in zip(sequence_ids, offsets, strict=True)
        if sequence_id == 1 and end > start
    ]


def _token_span_coverage(
    complete_offsets: list[tuple[int, int]],
    visible_offsets: list[tuple[int, int]],
    *,
    char_start: int,
    char_end: int,
) -> tuple[int, int, bool]:
    """用真实 token offset 判断 gold span 的 token 是否全部保留。"""
    complete = Counter(
        offset
        for offset in complete_offsets
        if offset[1] > char_start and offset[0] < char_end
    )
    visible = Counter(
        offset
        for offset in visible_offsets
        if offset[1] > char_start and offset[0] < char_end
    )
    total_tokens = sum(complete.values())
    visible_tokens = sum((complete & visible).values())
    return total_tokens, visible_tokens, total_tokens > 0 and visible_tokens == total_tokens


def _resolve_device(requested: str) -> str:
    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _resolve_dtype(requested: str, *, device: str) -> tuple[str, torch.dtype]:
    if requested == "auto":
        requested = "float16" if device in {"mps", "cuda"} else "float32"
    mapping = {"float16": torch.float16, "float32": torch.float32}
    if requested not in mapping:
        raise ValueError("RERANKER_DTYPE 只支持 auto、float16 或 float32")
    return requested, mapping[requested]


def get_scorer(request: Request) -> Scorer:
    scorer = getattr(request.app.state, "scorer", None)
    if scorer is None:
        raise HTTPException(status_code=503, detail="reranker 尚未就绪")
    return scorer


def create_app(scorer: Scorer | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if scorer is not None:
            app.state.scorer = scorer
        else:
            app.state.scorer = CrossEncoderScorer(ServiceSettings.from_env())
            # 在健康检查变为 ready 前完成一次设备/JIT warmup, 避免首个线上请求承担初始化延迟。
            app.state.scorer.score("warmup query", ["warmup document"])
        yield

    app = FastAPI(title="WorkPilot Reranker", version="0.1.0", lifespan=lifespan)

    @app.get("/health", response_model=HealthResponse)
    async def health(
        current: Annotated[Scorer, Depends(get_scorer)],
    ) -> HealthResponse:
        return HealthResponse(
            status="ok",
            model=current.model_name,
            revision=current.revision,
            device=current.device,
            dtype=current.dtype,
            batch_size=current.batch_size,
            max_length=current.max_length,
        )

    @app.post("/v1/rerank", response_model=RerankResponse)
    async def rerank(
        payload: RerankRequest,
        current: Annotated[Scorer, Depends(get_scorer)],
    ) -> RerankResponse:
        if payload.model is not None and payload.model != current.model_name:
            raise HTTPException(
                status_code=409,
                detail=f"服务已加载 {current.model_name}, 请求模型为 {payload.model}",
            )
        effective_max_length = payload.max_length or current.max_length
        if effective_max_length > current.max_length:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"请求 max_length={effective_max_length} 超过服务上限 "
                    f"{current.max_length}"
                ),
            )
        scores = current.score(
            payload.query,
            [document.text for document in payload.documents],
            max_length=effective_max_length,
        )
        span_audits = current.audit_spans(
            payload.query,
            payload.documents,
            max_length=effective_max_length,
        )
        ranked = sorted(
            enumerate(zip(payload.documents, scores, strict=True)),
            key=lambda item: (-item[1][1], item[0]),
        )[: payload.top_n]
        return RerankResponse(
            model=current.model_name,
            span_audits=span_audits,
            results=[
                RerankItem(index=index, id=document.id, relevance_score=score)
                for index, (document, score) in ranked
            ],
        )

    return app


app = create_app()
