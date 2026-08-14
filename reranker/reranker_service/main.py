import math
import os
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


class Document(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=20_000)


class RerankRequest(BaseModel):
    model: str | None = None
    query: str = Field(min_length=1, max_length=4_000)
    documents: list[Document] = Field(min_length=1, max_length=100)
    top_n: int = Field(default=10, ge=1, le=100)

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


class RerankResponse(BaseModel):
    model: str
    results: list[RerankItem]


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

    def score(self, query: str, documents: list[str]) -> list[float]: ...


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

    def score(self, query: str, documents: list[str]) -> list[float]:
        scores: list[float] = []
        with torch.inference_mode():
            for offset in range(0, len(documents), self.batch_size):
                batch = documents[offset : offset + self.batch_size]
                inputs = self._tokenizer(
                    [query] * len(batch),
                    batch,
                    padding=True,
                    truncation=True,
                    max_length=self.max_length,
                    return_tensors="pt",
                )
                inputs = {key: value.to(self.device) for key, value in inputs.items()}
                logits = self._model(**inputs, return_dict=True).logits.reshape(-1)
                scores.extend(torch.sigmoid(logits).float().cpu().tolist())
        if len(scores) != len(documents) or any(not math.isfinite(score) for score in scores):
            raise RuntimeError("reranker 返回了非法分数")
        return scores


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
        scores = current.score(payload.query, [document.text for document in payload.documents])
        ranked = sorted(
            enumerate(zip(payload.documents, scores, strict=True)),
            key=lambda item: (-item[1][1], item[0]),
        )[: payload.top_n]
        return RerankResponse(
            model=current.model_name,
            results=[
                RerankItem(index=index, id=document.id, relevance_score=score)
                for index, (document, score) in ranked
            ],
        )

    return app


app = create_app()
