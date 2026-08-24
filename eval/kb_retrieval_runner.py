"""在当前文件系统 KB 上运行可复现的检索评测。

与已经退役的 PostgreSQL runner 不同，本 runner 只走生产中的
``LocalKbService -> search_index`` 路径。gold 使用 content hash + 页码 + 页内字符区间，
因此可以在同一语料的不同索引版本之间稳定配对。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import subprocess
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any
from uuid import UUID, uuid4, uuid5

from app.core.config import Settings
from app.knowledge_contracts import KnowledgeUnavailableError
from app.rag.kb.index import KbHit, load_index, search_index
from app.rag.kb.manifest import KbDocument, KbIndexVersion, KbManifest
from app.rag.kb.service import LocalKbService
from llama_index.core.utils import get_tokenizer

from eval.kb_retrieval_suite import (
    KbRetrievalItem,
    KbRetrievalSuite,
    StableGoldSpan,
    load_kb_retrieval_suite,
    metric_char_offset,
    select_suite_items,
    stable_document_id,
    stable_source_id,
)
from eval.mapping import GoldSpan, RetrievedChunk
from eval.metrics.diagnostics import diagnose_spans, percentile, summarize_scores
from eval.metrics.refusal import analyze_refusal
from eval.metrics.retrieval import RetrievalMetrics, evaluate_retrieval

REPORT_SCHEMA_VERSION = 1
_NODE_NAMESPACE = UUID("a9a25757-b92f-480f-8bc6-05e5444a33de")
_SAFE_LABEL = re.compile(r"[^a-zA-Z0-9._-]+")
_RETRIEVAL_FIELDS = tuple(RetrievalMetrics.__dataclass_fields__)
_IMPLEMENTATION_FILES = (
    "backend/app/rag/kb/documents.py",
    "backend/app/rag/kb/index.py",
    "backend/app/rag/kb/manifest.py",
    "backend/app/rag/kb/service.py",
    "eval/kb_retrieval_runner.py",
    "eval/kb_retrieval_suite.py",
    "eval/mapping.py",
    "eval/metrics/diagnostics.py",
    "eval/metrics/refusal.py",
    "eval/metrics/retrieval.py",
    "eval/refusal_calibration.py",
)


def adaptive_rrf_min_score(*, rrf_k: int, consensus_rank: int) -> float:
    """两路至少在第 1 / consensus_rank 名形成共识时的最低 RRF 分数。"""

    if rrf_k < 1 or consensus_rank < 1:
        raise ValueError("rrf_k 和 consensus_rank 必须为正数")
    return 1.0 / rrf_k + 1.0 / (rrf_k + consensus_rank - 1)


# 拒答阈值的来源。`eval_best` 被显式列出来只是为了能明确拒绝它：拿本次评测集上
# macro-F1 最优的阈值回填进配置，等于在评测集上拟合，报出来的拒答指标不再有外推意义。
REFUSAL_THRESHOLD_SOURCES: frozenset[str] = frozenset(
    {"dev_calibrated", "independent_calibration", "manual"}
)
_FORBIDDEN_THRESHOLD_SOURCES: frozenset[str] = frozenset({"eval_best", "test_calibrated"})
REFUSAL_CALIBRATION_SCHEMA = "workpilot-refusal-calibration.v1"


@dataclass(frozen=True)
class RefusalCalibration:
    threshold: float
    score_source: str
    dataset: str
    dataset_sha256: str
    source_report_sha256: str
    method: str
    reviewer: str
    reviewed_at: str
    sha256: str

    def to_config(self) -> dict[str, object]:
        return {
            "schema_version": REFUSAL_CALIBRATION_SCHEMA,
            "dataset": self.dataset,
            "dataset_sha256": self.dataset_sha256,
            "source_report_sha256": self.source_report_sha256,
            "score_source": self.score_source,
            "threshold": self.threshold,
            "method": self.method,
            "reviewer": self.reviewer,
            "reviewed_at": self.reviewed_at,
            "sha256": self.sha256,
        }


def load_refusal_calibration(
    path: Path,
    *,
    evaluation_dataset_sha256: str,
) -> RefusalCalibration:
    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("schema_version") != REFUSAL_CALIBRATION_SCHEMA:
        raise ValueError(f"拒答校准文件必须是 {REFUSAL_CALIBRATION_SCHEMA}")
    integrity = payload.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("algorithm") != "sha256":
        raise ValueError("拒答校准文件缺少 sha256 完整性信息")
    unsigned = {key: value for key, value in payload.items() if key != "integrity"}
    if integrity.get("value") != _json_hash(unsigned):
        raise ValueError("拒答校准文件完整性校验失败")
    dataset_sha256 = str(payload.get("dataset_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", dataset_sha256):
        raise ValueError("拒答校准 dataset_sha256 非法")
    if dataset_sha256 == evaluation_dataset_sha256:
        raise ValueError("拒答阈值不能在本次 evaluation suite 自身上标定")
    source_report_sha256 = str(payload.get("source_report_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", source_report_sha256):
        raise ValueError("拒答校准 source_report_sha256 非法")
    threshold_raw = payload.get("threshold")
    if isinstance(threshold_raw, bool) or not isinstance(threshold_raw, int | float):
        raise TypeError("拒答校准 threshold 必须是有限数字")
    threshold = float(threshold_raw)
    if not math.isfinite(threshold):
        raise ValueError("拒答校准 threshold 必须是有限数字")
    score_source = str(payload.get("score_source") or "")
    if score_source not in {"rerank", "fusion", "dense", "lexical"}:
        raise ValueError("拒答校准 score_source 非法")
    reviewer = str(payload.get("reviewer") or "").strip()
    reviewed_at = str(payload.get("reviewed_at") or "").strip()
    method = str(payload.get("method") or "").strip()
    dataset = str(payload.get("dataset") or "").strip()
    if not reviewer or not reviewed_at or not method or not dataset:
        raise ValueError("拒答校准缺少 dataset/method/reviewer/reviewed_at")
    try:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("拒答校准 reviewed_at 必须是 ISO-8601 时间") from error
    if parsed.tzinfo is None:
        raise ValueError("拒答校准 reviewed_at 必须包含时区")
    return RefusalCalibration(
        threshold=threshold,
        score_source=score_source,
        dataset=dataset,
        dataset_sha256=dataset_sha256,
        source_report_sha256=source_report_sha256,
        method=method,
        reviewer=reviewer,
        reviewed_at=reviewed_at,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def validate_refusal_threshold(
    *,
    threshold: float | None,
    source: str | None,
    score_source: str | None,
    observed_scores: Sequence[float],
) -> None:
    """fail-closed 地把拒答阈值绑到具体排序器的量纲和一个合法来源上。

    dense cosine、RRF 与 cross-encoder 不共享量纲：把 0.35 这种为归一化打分器
    定的阈值套到 RRF 分数（上界约 2/rrf_k）上，会把全部可答题判成拒答，而所有
    检索指标照常输出——这是无声失败，必须挡在写报告之前。
    """

    if threshold is None:
        return
    if source not in REFUSAL_THRESHOLD_SOURCES:
        if source in _FORBIDDEN_THRESHOLD_SOURCES:
            raise ValueError(
                f"refusal_threshold_source={source} 不允许：该阈值取自评测集自身的最优点，"
                "属于在评测集上拟合。请在 dev split 上标定后传 --refusal-threshold-source dev_calibrated"
            )
        raise ValueError(
            "设置 --refusal-threshold 时必须同时声明 --refusal-threshold-source，"
            f"取值范围 {sorted(REFUSAL_THRESHOLD_SOURCES)}，例如 dev_calibrated"
        )
    if not score_source:
        raise ValueError(
            "无法确定 top_score 的排序器来源，拒绝套用拒答阈值。"
            "检查 config.retrieval_score_source 是否为空"
        )
    finite = [float(score) for score in observed_scores]
    if not finite:
        raise ValueError("本次跑批没有任何 top_score，无法校验拒答阈值是否落在该排序器的量纲内")
    low, high = min(finite), max(finite)
    if not low <= threshold <= high:
        raise ValueError(
            f"拒答阈值 {threshold:g} 落在 {score_source} 分数的实测区间 "
            f"[{low:.6g}, {high:.6g}] 之外，套用它会把几乎所有题判到同一侧。"
            f"请在 dev split 上按 {score_source} 的量纲重新标定阈值"
        )


def resolve_actual_score_source(
    observations: Sequence[dict[str, Any]],
    *,
    rerank_required: bool,
) -> str:
    """从真实命中结果确定分数量纲；评测不允许静默混用或 rerank 降级。"""

    sources = {
        str(item["retrieval_score_source"])
        for item in observations
        if item.get("error") is None and item.get("retrieval_score_source")
    }
    if not sources:
        raise ValueError("本次跑批没有可确认的 retrieval_score_source")
    if len(sources) != 1:
        raise ValueError(f"同一次评测混用了多个检索分数量纲: {sorted(sources)}")
    actual = next(iter(sources))
    if rerank_required and actual != "rerank":
        raise ValueError(
            f"配置要求 rerank，但真实 score_source={actual}；reranker fallback 不能用于正式评测"
        )
    return actual


@dataclass(frozen=True)
class IndexedNode:
    node_id: str
    chunk_id: UUID
    source_id: UUID
    document_id: UUID
    content_hash: str
    filename: str
    page_no: int | None
    char_start: int
    char_end: int
    text: str
    content_tokens: int

    @property
    def metric_char_start(self) -> int:
        return metric_char_offset(self.page_no, self.char_start)

    @property
    def metric_char_end(self) -> int:
        return metric_char_offset(self.page_no, self.char_end)

    def to_metric_chunk(self, *, score: float) -> RetrievedChunk:
        return RetrievedChunk(
            chunk_id=self.chunk_id,
            version_id=self.document_id,
            char_start=self.metric_char_start,
            char_end=self.metric_char_end,
            content_tokens=self.content_tokens,
            score=score,
        )

    def to_report(self, *, score: float, rank: int) -> dict[str, object]:
        return {
            "rank": rank,
            "chunk_id": str(self.chunk_id),
            # 兼容现有 compare/gate：version_id 是内容哈希派生的稳定文档 UUID；字符区间
            # 使用页码编码后的全局坐标。页面内的可读坐标另行保留，避免 3 页被算成 3 篇文档。
            "version_id": str(self.document_id),
            "content_hash": self.content_hash,
            "filename": self.filename,
            "page_no": self.page_no,
            "char_start": self.metric_char_start,
            "char_end": self.metric_char_end,
            "page_char_start": self.char_start,
            "page_char_end": self.char_end,
            "content_tokens": self.content_tokens,
            "score": score,
        }


@dataclass(frozen=True)
class IndexCatalog:
    manifest: KbManifest
    version: KbIndexVersion
    index_path: Path
    nodes: tuple[IndexedNode, ...]

    @property
    def by_node_id(self) -> dict[str, IndexedNode]:
        return {node.node_id: node for node in self.nodes}

    @property
    def metric_candidates(self) -> list[RetrievedChunk]:
        return [node.to_metric_chunk(score=0.0) for node in self.nodes]

    def metric_retrieved(self, hits: Sequence[KbHit]) -> list[RetrievedChunk]:
        by_id = self.by_node_id
        result: list[RetrievedChunk] = []
        for hit in hits:
            node = by_id.get(hit.node_id)
            if node is None:
                raise ValueError(f"检索返回了 docstore 中不存在的节点: {hit.node_id}")
            result.append(node.to_metric_chunk(score=hit.score))
        return result

    def report_retrieved(self, hits: Sequence[KbHit]) -> list[dict[str, object]]:
        by_id = self.by_node_id
        return [
            {
                **by_id[hit.node_id].to_report(score=hit.score, rank=rank),
                "score_source": hit.score_source,
            }
            for rank, hit in enumerate(hits, start=1)
        ]

    def validate_span(self, span: StableGoldSpan) -> None:
        if span.content_hash not in self.version.document_hashes:
            raise ValueError(
                f"gold content_hash {span.content_hash[:12]} 不在索引版本 "
                f"{self.version.version_id} 的文档集合中"
            )
        source_nodes = [node for node in self.nodes if node.source_id == span.source_id]
        if not source_nodes:
            locator = "文本" if span.page_no is None else f"第 {span.page_no} 页"
            raise ValueError(
                f"索引中找不到 {span.content_hash[:12]} 的{locator}；页码或索引版本可能错了"
            )

        actual: list[str | None] = [None] * (span.char_end - span.char_start)
        for node in source_nodes:
            overlap_start = max(span.char_start, node.char_start)
            overlap_end = min(span.char_end, node.char_end)
            if overlap_end <= overlap_start:
                continue
            node_offset = overlap_start - node.char_start
            target_offset = overlap_start - span.char_start
            fragment = node.text[node_offset : node_offset + overlap_end - overlap_start]
            if len(fragment) != overlap_end - overlap_start:
                raise ValueError(f"节点 {node.node_id} 的字符区间与正文长度不一致，重建索引")
            for index, char in enumerate(fragment, start=target_offset):
                previous = actual[index]
                if previous is not None and previous != char:
                    raise ValueError(f"节点 {node.node_id} 的重叠字符不一致，索引可能已损坏")
                actual[index] = char
        if any(char is None for char in actual):
            raise ValueError(
                f"gold span {span.content_hash[:12]}:{span.page_no}:"
                f"{span.char_start}-{span.char_end} 没有被当前索引完整覆盖"
            )
        reconstructed = "".join(char for char in actual if char is not None)
        if reconstructed != span.quote:
            raise ValueError(
                f"gold span 已漂移: {span.content_hash[:12]}:{span.page_no}:"
                f"{span.char_start}-{span.char_end} 实际为 {reconstructed[:80]!r}，"
                f"标注为 {span.quote[:80]!r}"
            )

    def find_quote(self, quote: str) -> list[dict[str, object]]:
        if not quote:
            raise ValueError("quote 不能为空")
        matches: dict[tuple[str, int | None, int, int], dict[str, object]] = {}
        for node in self.nodes:
            offset = 0
            while True:
                found = node.text.find(quote, offset)
                if found < 0:
                    break
                start = node.char_start + found
                key = (node.content_hash, node.page_no, start, start + len(quote))
                matches[key] = {
                    "content_hash": node.content_hash,
                    "filename": node.filename,
                    "page_no": node.page_no,
                    "char_start": start,
                    "char_end": start + len(quote),
                    "quote": quote,
                }
                offset = found + 1
        return [
            matches[key]
            for key in sorted(matches, key=lambda item: (item[0], item[1] or 0, item[2]))
        ]


def load_catalog(
    service: LocalKbService,
    *,
    settings: Settings,
    kb_slug: str,
    kb_version_id: str | None,
) -> IndexCatalog:
    manifest = service.get(kb_slug)
    version = service.resolve_version(manifest, kb_version_id)
    path = service.index_path(kb_slug, version.version_id)
    index = load_index(path, settings)
    documents = _documents_by_node_doc_id(manifest, version)
    tokenizer = get_tokenizer()
    nodes: list[IndexedNode] = []
    for raw_node in index.docstore.docs.values():
        metadata = getattr(raw_node, "metadata", None) or {}
        raw_doc_id = str(metadata.get("doc_id") or "")
        document = documents.get(raw_doc_id)
        if document is None:
            # docstore 也可能持有不参与检索的辅助节点；没有产品 provenance 的节点不能进入
            # 评测，因为它无法映射到稳定内容锚点。
            continue
        start = getattr(raw_node, "start_char_idx", None)
        end = getattr(raw_node, "end_char_idx", None)
        if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int):
            raise TypeError(f"节点 {raw_node.node_id} 没有字符区间；用当前版本重建 KB 索引后再评测")
        text = str(raw_node.get_content())
        if end <= start or len(text) != end - start:
            raise ValueError(
                f"节点 {raw_node.node_id} 的字符区间 {start}-{end} 与正文长度不一致；重建索引"
            )
        raw_page = metadata.get("page_no")
        page_no = raw_page if isinstance(raw_page, int) and not isinstance(raw_page, bool) else None
        node_id = str(raw_node.node_id)
        nodes.append(
            IndexedNode(
                node_id=node_id,
                chunk_id=_node_uuid(node_id),
                source_id=stable_source_id(document.content_hash, page_no),
                document_id=stable_document_id(document.content_hash),
                content_hash=document.content_hash,
                filename=document.filename,
                page_no=page_no,
                char_start=start,
                char_end=end,
                text=text,
                content_tokens=len(tokenizer(text)),
            )
        )
    if not nodes:
        raise ValueError("索引 docstore 中没有带稳定 provenance 的可评测节点；请重建索引")
    if len({node.node_id for node in nodes}) != len(nodes):
        raise ValueError("索引包含重复 node_id，无法稳定评测")
    return IndexCatalog(manifest=manifest, version=version, index_path=path, nodes=tuple(nodes))


def _documents_by_node_doc_id(
    manifest: KbManifest,
    version: KbIndexVersion,
) -> dict[str, KbDocument]:
    covered = set(version.document_hashes)
    result: dict[str, KbDocument] = {}
    for document in manifest.documents:
        if document.content_hash not in covered:
            continue
        if (
            document.doc_id in result
            and result[document.doc_id].content_hash != document.content_hash
        ):
            raise ValueError(f"doc_id 前缀碰撞: {document.doc_id}；重建时需要扩大内容哈希前缀")
        result[document.doc_id] = document
    missing = covered - {document.content_hash for document in result.values()}
    if missing:
        shown = ", ".join(sorted(value[:12] for value in missing))
        raise ValueError(f"manifest 已不包含版本 {version.version_id} 引用的文档: {shown}")
    return result


def _node_uuid(node_id: str) -> UUID:
    try:
        return UUID(node_id)
    except ValueError:
        return uuid5(_NODE_NAMESPACE, node_id)


async def run_evaluation(
    *,
    suite: KbRetrievalSuite,
    items: Sequence[KbRetrievalItem],
    service: LocalKbService,
    settings: Settings,
    kb_slug: str,
    kb_version_id: str | None,
    label: str,
    top_k: int,
    diagnostic_k: int,
    token_budget: int,
    theta: float,
    alpha: float,
    refusal_threshold: float | None,
    refusal_threshold_source: str | None,
    output_dir: Path | None,
    include_test: bool,
    test_access_note: str | None,
    refusal_calibration: RefusalCalibration | None = None,
    rrf_lexical_weight: float | None = None,
    adaptive_top_k_enabled: bool = False,
    adaptive_max_top_k: int = 10,
    adaptive_consensus_rank: int = 4,
) -> tuple[Path, dict[str, Any]]:
    _validate_run_config(
        label=label,
        top_k=top_k,
        diagnostic_k=diagnostic_k,
        token_budget=token_budget,
        theta=theta,
        alpha=alpha,
        adaptive_top_k_enabled=adaptive_top_k_enabled,
        adaptive_max_top_k=adaptive_max_top_k,
        adaptive_consensus_rank=adaptive_consensus_rank,
    )
    repo_root = Path(__file__).resolve().parents[1]
    package = output_dir or _default_output_dir(repo_root, label)
    if package.exists():
        raise ValueError(f"输出目录已存在，拒绝覆盖: {package}")
    catalog = load_catalog(
        service,
        settings=settings,
        kb_slug=kb_slug,
        kb_version_id=kb_version_id,
    )
    if rrf_lexical_weight is not None:
        if rrf_lexical_weight <= 0:
            raise ValueError("rrf_lexical_weight 必须大于 0")
        catalog = replace(
            catalog,
            version=replace(
                catalog.version,
                retrieval=replace(
                    catalog.version.retrieval,
                    rrf_lexical_weight=rrf_lexical_weight,
                ),
            ),
        )
    if adaptive_top_k_enabled:
        if catalog.version.retrieval.engine != "hybrid":
            raise ValueError("adaptive Top-K 当前只支持 hybrid RRF")
        if settings.rerank_enabled:
            raise ValueError("adaptive Top-K 无 reranker 实验要求 RERANK_ENABLED=false")
    for item in items:
        for span in item.spans:
            catalog.validate_span(span)

    if refusal_calibration is None:
        if refusal_threshold is not None or refusal_threshold_source is not None:
            raise ValueError("正式评测设置拒答阈值时必须提供独立 --refusal-calibration 文件")
    else:
        if refusal_threshold is not None or refusal_threshold_source is not None:
            raise ValueError("--refusal-calibration 不能与直接阈值参数同时使用")
        if refusal_calibration.dataset_sha256 == suite.sha256:
            raise ValueError("拒答阈值不能在本次 evaluation suite 自身上标定")
        refusal_threshold = refusal_calibration.threshold
        refusal_threshold_source = "independent_calibration"

    config: dict[str, Any] = {
        "strategy": catalog.version.retrieval.engine,
        "origin": suite.origin,
        "top_k": top_k,
        "diagnostic_k": diagnostic_k,
        "token_budget": token_budget,
        "theta": theta,
        "alpha": alpha,
        "refusal_threshold": refusal_threshold,
        "refusal_threshold_source": refusal_threshold_source,
        "refusal_calibration": (
            refusal_calibration.to_config() if refusal_calibration is not None else None
        ),
        "kb_slug": kb_slug,
        "kb_version_id": catalog.version.version_id,
        "embedding": catalog.version.embedding.to_dict(),
        "retrieval": catalog.version.retrieval.to_dict(),
        "rerank": {
            "enabled": settings.rerank_enabled,
            "candidate_k": settings.rerank_candidate_k,
            "model": settings.reranker_model,
            "max_candidate_chars": settings.rerank_max_candidate_chars,
            "candidate_text_mode": settings.rerank_candidate_text_mode,
        },
        "adaptive_top_k": {
            "enabled": adaptive_top_k_enabled,
            "max_top_k": adaptive_max_top_k,
            "trigger": "rrf_route_consensus",
            "consensus_rank": adaptive_consensus_rank,
            "min_score": adaptive_rrf_min_score(
                rrf_k=catalog.version.retrieval.rrf_k,
                consensus_rank=adaptive_consensus_rank,
            ),
        },
        "requested_retrieval_score_source": (
            "rerank"
            if settings.rerank_enabled
            else (
                "fusion"
                if catalog.version.retrieval.engine == "hybrid"
                else catalog.version.retrieval.engine
            )
        ),
        "token_count_mode": "llama_index_default_tokenizer",
    }
    run_id = str(uuid4())
    started_at = datetime.now(UTC)
    observations: list[dict[str, Any]] = []
    for item in items:
        observations.append(
            await _evaluate_item(
                item,
                catalog=catalog,
                settings=settings,
                top_k=top_k,
                diagnostic_k=diagnostic_k,
                token_budget=token_budget,
                theta=theta,
                alpha=alpha,
                adaptive_top_k_enabled=adaptive_top_k_enabled,
                adaptive_max_top_k=adaptive_max_top_k,
                adaptive_consensus_rank=adaptive_consensus_rank,
            )
        )
    finished_at = datetime.now(UTC)
    actual_score_source = resolve_actual_score_source(
        observations,
        rerank_required=settings.rerank_enabled,
    )
    if (
        refusal_calibration is not None
        and refusal_calibration.score_source != actual_score_source
    ):
        raise ValueError(
            "拒答校准与本次运行分数量纲不一致: "
            f"calibration={refusal_calibration.score_source}, actual={actual_score_source}"
        )
    config["retrieval_score_source"] = actual_score_source
    config_hash = _json_hash(config)
    validate_refusal_threshold(
        threshold=refusal_threshold,
        source=refusal_threshold_source,
        score_source=actual_score_source,
        observed_scores=[
            float(item["top_score"])
            for item in observations
            if item.get("error") is None and item.get("top_score") is not None
        ],
    )
    git_sha, git_dirty = _git_state(repo_root)
    report: dict[str, Any] = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "kind": "retrieval",
        "run_id": run_id,
        "dataset": suite.name,
        "label": label,
        "git_sha": git_sha,
        "config": config,
        "config_hash": config_hash,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": round((finished_at - started_at).total_seconds() * 1000, 3),
        "suite": {
            "path": str(suite.source_path),
            "sha256": suite.sha256,
            "description": suite.description,
            "origin": suite.origin,
            "review_status": suite.review_status,
            "reviewer": suite.reviewer,
            "reviewed_at": suite.reviewed_at,
            "selected_items": len(items),
            "include_test": include_test,
            "test_access_note": test_access_note if include_test else None,
        },
        "kb": {
            "slug": catalog.manifest.slug,
            "version_id": catalog.version.version_id,
            "version_label": catalog.version.label,
            "version_created_at": catalog.version.created_at,
            "document_hashes": list(catalog.version.document_hashes),
            "node_count": len(catalog.nodes),
            "is_stale": not catalog.version.covers(catalog.manifest.document_hashes),
            "index_fingerprint": _tree_fingerprint(catalog.index_path),
        },
        "reproducibility": {
            "git_dirty": git_dirty,
            "implementation_fingerprint": _implementation_fingerprint(repo_root),
        },
        "metrics": _summarize(observations, refusal_threshold=refusal_threshold),
        "items": observations,
    }
    package.mkdir(parents=True, exist_ok=False)
    (package / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (package / "report.md").write_text(_render_markdown(report), encoding="utf-8")
    return package, report


async def _evaluate_item(
    item: KbRetrievalItem,
    *,
    catalog: IndexCatalog,
    settings: Settings,
    top_k: int,
    diagnostic_k: int,
    token_budget: int,
    theta: float,
    alpha: float,
    adaptive_top_k_enabled: bool,
    adaptive_max_top_k: int,
    adaptive_consensus_rank: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        initial_hits = await search_index(
            catalog.index_path,
            item.question,
            settings=settings,
            version=catalog.version,
            top_k=top_k,
        )
        initial_top_score = initial_hits[0].score if initial_hits else 0.0
        adaptive_min_score = adaptive_rrf_min_score(
            rrf_k=catalog.version.retrieval.rrf_k,
            consensus_rank=adaptive_consensus_rank,
        )
        adaptive_expanded = adaptive_top_k_enabled and (
            not initial_hits or initial_top_score < adaptive_min_score
        )
        hits = initial_hits
        if adaptive_expanded:
            hits = await search_index(
                catalog.index_path,
                item.question,
                settings=settings,
                version=catalog.version,
                top_k=adaptive_max_top_k,
            )
        effective_top_k = adaptive_max_top_k if adaptive_expanded else top_k
        retrieval_finished = time.perf_counter()
        retrieved = catalog.metric_retrieved(hits)
        retrieval: dict[str, float | int] | None = None
        diagnostics: list[dict[str, object]] = []
        diagnostic_hits: list[KbHit] = []
        if item.answerable:
            metric_groups = [group.to_metric_group() for group in item.evidence_groups]
            metric_spans = _unique_metric_spans(
                alternative for group in metric_groups for alternative in group.alternatives
            )
            measured = evaluate_retrieval(
                metric_spans,
                retrieved,
                catalog.metric_candidates,
                top_k=effective_top_k,
                token_budget=token_budget,
                theta=theta,
                alpha=alpha,
                gold_evidence_groups=metric_groups,
            )
            retrieval = measured.to_dict()
            # 正式指标必须严格走生产 Top-K。只有正式结果确实漏证据时，才额外跑一条
            # 不带 rerank 的 diagnostic-K 上游检索做归因；它不能反过来污染正式排序。
            diagnostic_retrieved = retrieved
            if measured.span_recall_at_k < 1.0 and diagnostic_k > effective_top_k:
                diagnostic_settings = settings.model_copy(update={"rerank_enabled": False})
                diagnostic_hits = await search_index(
                    catalog.index_path,
                    item.question,
                    settings=diagnostic_settings,
                    version=catalog.version,
                    top_k=diagnostic_k,
                )
                diagnostic_retrieved = catalog.metric_retrieved(diagnostic_hits)
            raw_diagnostics = diagnose_spans(
                metric_spans,
                retrieved,
                catalog.metric_candidates,
                top_k=effective_top_k,
                token_budget=token_budget,
                theta=theta,
                evidence_groups=metric_groups,
                diagnostic_retrieved=diagnostic_retrieved,
            )
            diagnostics = [
                {
                    **diagnostic.to_dict(),
                    **item.evidence_groups[index].alternatives[0].to_dict(),
                }
                for index, diagnostic in enumerate(raw_diagnostics)
            ]
        return {
            "item_id": item.item_id,
            "split": item.split,
            "category": item.category,
            "question": item.question,
            "answerable": item.answerable,
            "top_score": hits[0].score if hits else 0.0,
            "initial_top_score": initial_top_score,
            "effective_top_k": effective_top_k,
            "adaptive_expanded": adaptive_expanded,
            "latency_ms": round((retrieval_finished - started) * 1000, 3),
            "retrieval_score_source": hits[0].score_source if hits else None,
            "retrieval": retrieval,
            "retrieved": catalog.report_retrieved(hits),
            "diagnostic_retrieved": catalog.report_retrieved(diagnostic_hits),
            "span_diagnostics": diagnostics,
            "error": None,
        }
    except KnowledgeUnavailableError:
        # embedding 端点、索引签名或整条检索基础设施不可用不是某一道题的失败。立即停止，
        # 避免同一个端点故障在 N 条样本上逐条重试数分钟。
        raise
    except Exception as error:
        return {
            "item_id": item.item_id,
            "split": item.split,
            "category": item.category,
            "question": item.question,
            "answerable": item.answerable,
            "top_score": None,
            "initial_top_score": None,
            "effective_top_k": top_k,
            "adaptive_expanded": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "retrieval_score_source": None,
            "retrieval": None,
            "retrieved": [],
            "diagnostic_retrieved": [],
            "span_diagnostics": [
                {
                    "fact_id": group.fact_id,
                    **group.alternatives[0].to_dict(),
                }
                for group in item.evidence_groups
            ],
            "error": f"{type(error).__name__}: {error}",
        }


def _unique_metric_spans(spans: Iterable[GoldSpan]) -> list[GoldSpan]:
    result: list[GoldSpan] = []
    seen: set[tuple[UUID, int, int]] = set()
    for span in spans:
        key = (span.version_id, span.char_start, span.char_end)
        if key not in seen:
            seen.add(key)
            result.append(span)
    return result


def _summarize(
    items: Sequence[dict[str, Any]],
    *,
    refusal_threshold: float | None,
) -> dict[str, Any]:
    completed = [item for item in items if item.get("error") is None]
    answerable = [item for item in completed if isinstance(item.get("retrieval"), dict)]
    summary: dict[str, Any] = {
        "item_count": len(items),
        "completed_count": len(completed),
        "error_count": len(items) - len(completed),
        "answerable_count": sum(bool(item.get("answerable")) for item in completed),
        "unanswerable_count": sum(not bool(item.get("answerable")) for item in completed),
        "rerank_applied_count": sum(
            item.get("retrieval_score_source") == "rerank" for item in completed
        ),
        "adaptive_expanded_count": sum(bool(item.get("adaptive_expanded")) for item in completed),
    }
    for field in _RETRIEVAL_FIELDS:
        values = [float(item["retrieval"][field]) for item in answerable]
        summary[field] = fmean(values) if values else None
    latencies = [float(item["latency_ms"]) for item in completed]
    summary["latency_ms_mean"] = fmean(latencies) if latencies else None
    summary["latency_ms_p95"] = percentile(sorted(latencies), 0.95)
    scores = [
        (float(item["top_score"]), bool(item["answerable"]))
        for item in completed
        if item.get("top_score") is not None
    ]
    summary["refusal"] = analyze_refusal(
        scores,
        configured_threshold=refusal_threshold,
    ).to_dict()
    summary["score_distribution"] = {
        "answerable": summarize_scores([score for score, label in scores if label]),
        "unanswerable": summarize_scores([score for score, label in scores if not label]),
    }
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_category[str(item.get("category") or "unknown")].append(item)
    summary["by_category"] = {
        category: _category_summary(category_items)
        for category, category_items in sorted(by_category.items())
    }
    return summary


def _category_summary(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    completed = [item for item in items if item.get("error") is None]
    answerable = [item for item in completed if isinstance(item.get("retrieval"), dict)]
    return {
        "item_count": len(items),
        "completed_count": len(completed),
        "span_recall_at_k": _mean_retrieval(answerable, "span_recall_at_k"),
        "budget_span_recall": _mean_retrieval(answerable, "budget_span_recall"),
        "ndcg_at_k": _mean_retrieval(answerable, "ndcg_at_k"),
        "context_precision": _mean_retrieval(answerable, "context_precision"),
        "latency_ms_mean": (
            fmean(float(item["latency_ms"]) for item in completed) if completed else None
        ),
    }


def _mean_retrieval(items: Sequence[dict[str, Any]], field: str) -> float | None:
    values = [float(item["retrieval"][field]) for item in items]
    return fmean(values) if values else None


def _validate_run_config(
    *,
    label: str,
    top_k: int,
    diagnostic_k: int,
    token_budget: int,
    theta: float,
    alpha: float,
    adaptive_top_k_enabled: bool,
    adaptive_max_top_k: int,
    adaptive_consensus_rank: int,
) -> None:
    if not label.strip():
        raise ValueError("label 不能为空")
    if top_k < 1 or diagnostic_k < top_k:
        raise ValueError("top_k 必须为正数，且 diagnostic_k 不能小于 top_k")
    if token_budget < 1:
        raise ValueError("token_budget 必须为正数")
    if not 0 < theta <= 1:
        raise ValueError("theta 必须位于 (0,1]")
    if not 0 <= alpha < 1:
        raise ValueError("alpha 必须位于 [0,1)")
    if adaptive_max_top_k < top_k:
        raise ValueError("adaptive_max_top_k 不能小于 top_k")
    if adaptive_top_k_enabled and diagnostic_k < adaptive_max_top_k:
        raise ValueError("adaptive Top-K 开启时 diagnostic_k 不能小于 adaptive_max_top_k")
    if adaptive_consensus_rank < 1:
        raise ValueError("adaptive_consensus_rank 必须为正数")


def _json_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _implementation_fingerprint(repo_root: Path) -> str:
    digest = hashlib.sha256()
    for relative in _IMPLEMENTATION_FILES:
        path = repo_root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git_state(repo_root: Path) -> tuple[str | None, bool]:
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return (sha.stdout.strip() or None, bool(status.stdout.strip()))


def _default_output_dir(repo_root: Path, label: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe = _SAFE_LABEL.sub("-", label.strip()).strip("-.") or "run"
    return repo_root / "eval" / "outputs" / "kb-retrieval" / f"{timestamp}-{safe}"


def _render_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    config = report["config"]
    kb = report["kb"]
    lines = [
        f"# KB 检索评测：{report['label']}",
        "",
        f"- 数据集：`{report['dataset']}`（{metrics['item_count']} 条）",
        f"- 知识库：`{kb['slug']}:{kb['version_id']}` / `{config['strategy']}`",
        f"- 配置哈希：`{str(report['config_hash'])[:16]}`",
        f"- 套件哈希：`{str(report['suite']['sha256'])[:16]}`",
        f"- 完成：{metrics['completed_count']}，失败：{metrics['error_count']}",
        "",
        "## 总指标",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| span Recall@K | {_ratio(metrics['span_recall_at_k'])} |",
        f"| budget span Recall | {_ratio(metrics['budget_span_recall'])} |",
        f"| nDCG@K | {_ratio(metrics['ndcg_at_k'])} |",
        f"| α-nDCG@K | {_ratio(metrics['alpha_ndcg_at_k'])} |",
        f"| context precision | {_ratio(metrics['context_precision'])} |",
        f"| gold 文档 Recall@K | {_ratio(metrics['gold_doc_recall_at_k'])} |",
        f"| P95 延迟 | {_number(metrics['latency_ms_p95'], ' ms')} |",
        "",
        "## 拒答信号",
        "",
    ]
    refusal = metrics["refusal"]
    best = refusal.get("best") or {}
    lines.extend(
        [
            f"- AUROC：{_number(refusal.get('auroc'))}",
            f"- dev 最优阈值：{_number(best.get('threshold'))}",
            f"- 最优 macro-F1：{_ratio(best.get('macro_f1'))}",
            "",
            "## 分类结果",
            "",
            "| 分类 | 条数 | Recall@K | budget Recall | nDCG@K | 延迟均值 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for category, values in metrics["by_category"].items():
        lines.append(
            f"| {category} | {values['item_count']} | {_ratio(values['span_recall_at_k'])} | "
            f"{_ratio(values['budget_span_recall'])} | {_ratio(values['ndcg_at_k'])} | "
            f"{_number(values['latency_ms_mean'], ' ms')} |"
        )
    misses = [
        (item["item_id"], diagnostic)
        for item in report["items"]
        for diagnostic in item.get("span_diagnostics") or []
        if diagnostic.get("status") not in {None, "hit"}
    ]
    lines.extend(["", "## 未命中归因", ""])
    if not misses:
        lines.append("无。")
    else:
        for item_id, diagnostic in misses:
            lines.append(
                f"- `{item_id}` / `{diagnostic.get('fact_id')}`：{diagnostic.get('status')}"
            )
    return "\n".join(lines) + "\n"


def _ratio(value: Any) -> str:
    return "N/A" if value is None else f"{float(value):.2%}"


def _number(value: Any, suffix: str = "") -> str:
    return "N/A" if value is None else f"{float(value):.3f}{suffix}"


def _settings_and_service(kb_root: Path | None) -> tuple[Settings, LocalKbService]:
    settings = Settings()
    root = (kb_root or settings.knowledge_base_path).expanduser()
    if kb_root is not None:
        settings = settings.model_copy(update={"knowledge_base_path": root})
    return settings, LocalKbService(root, settings=settings)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    anchors = subparsers.add_parser("anchors", help="在指定索引中查找逐字 quote 的稳定锚点")
    _add_kb_args(anchors)
    anchors.add_argument("--quote", required=True)

    run = subparsers.add_parser("run", help="运行检索评测并生成 JSON/Markdown 报告")
    _add_kb_args(run)
    run.add_argument("--suite", type=Path, required=True)
    run.add_argument("--label", required=True)
    run.add_argument("--top-k", type=int, default=10)
    run.add_argument("--diagnostic-k", type=int, default=50)
    run.add_argument("--token-budget", type=int, default=4000)
    run.add_argument("--theta", type=float, default=0.5)
    run.add_argument("--alpha", type=float, default=0.5)
    run.add_argument("--refusal-threshold", type=float)
    run.add_argument(
        "--refusal-threshold-source",
        choices=sorted(REFUSAL_THRESHOLD_SOURCES),
        help="拒答阈值的标定来源；设置 --refusal-threshold 时必填，禁止回填评测集上的最优阈值",
    )
    run.add_argument(
        "--refusal-calibration",
        type=Path,
        help="独立 calibration suite 生成并经人工复核的阈值文件；正式 baseline 必须使用",
    )
    run.add_argument("--rrf-lexical-weight", type=float)
    run.add_argument("--adaptive-top-k", action="store_true")
    run.add_argument("--adaptive-max-top-k", type=int, default=10)
    run.add_argument("--adaptive-consensus-rank", type=int, default=4)
    run.add_argument("--output-dir", type=Path)
    run.add_argument("--allow-synthetic", action="store_true")
    run.add_argument("--include-test", action="store_true")
    run.add_argument("--test-access-note")
    return parser


def _add_kb_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kb-root", type=Path)
    parser.add_argument("--kb-slug", required=True)
    parser.add_argument("--kb-version")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        settings, service = _settings_and_service(args.kb_root)
        if args.command == "anchors":
            catalog = load_catalog(
                service,
                settings=settings,
                kb_slug=args.kb_slug,
                kb_version_id=args.kb_version,
            )
            matches = catalog.find_quote(args.quote)
            print(json.dumps({"matches": matches}, ensure_ascii=False, indent=2))
            return 0 if matches else 1

        suite = load_kb_retrieval_suite(args.suite, allow_synthetic=args.allow_synthetic)
        refusal_calibration = (
            load_refusal_calibration(
                args.refusal_calibration,
                evaluation_dataset_sha256=suite.sha256,
            )
            if args.refusal_calibration is not None
            else None
        )
        items = select_suite_items(
            suite,
            include_test=args.include_test,
            test_access_note=args.test_access_note,
        )
        package, report = asyncio.run(
            run_evaluation(
                suite=suite,
                items=items,
                service=service,
                settings=settings,
                kb_slug=args.kb_slug,
                kb_version_id=args.kb_version,
                label=args.label,
                top_k=args.top_k,
                diagnostic_k=args.diagnostic_k,
                token_budget=args.token_budget,
                theta=args.theta,
                alpha=args.alpha,
                refusal_threshold=args.refusal_threshold,
                refusal_threshold_source=args.refusal_threshold_source,
                refusal_calibration=refusal_calibration,
                output_dir=args.output_dir,
                include_test=args.include_test,
                test_access_note=args.test_access_note,
                rrf_lexical_weight=args.rrf_lexical_weight,
                adaptive_top_k_enabled=args.adaptive_top_k,
                adaptive_max_top_k=args.adaptive_max_top_k,
                adaptive_consensus_rank=args.adaptive_consensus_rank,
            )
        )
        print(package / "report.md")
        return 1 if report["metrics"]["error_count"] else 0
    except (KnowledgeUnavailableError, OSError, TypeError, ValueError) as error:
        print(f"评测拒绝运行：{error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "IndexCatalog",
    "IndexedNode",
    "load_catalog",
    "main",
    "run_evaluation",
]
