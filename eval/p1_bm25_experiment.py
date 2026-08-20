"""P1-G：以 BM25-50 替换 ts_rank-50 的严格单变量对照。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm_bootstrap import build_model_gateway
from app.rag.grounded_answer import evaluate_refusal
from app.rag.retrieval.dense import DenseSearchHit, _dense_search_by_vector
from app.rag.retrieval.fusion import reciprocal_rank_fusion, rerank_candidate_union
from app.rag.retrieval.lexical import lexical_search
from eval.dense_baseline import EvalItem, _candidate_chunks, _load_items, _retrieved_chunk
from eval.mapping import RetrievedChunk
from eval.metrics.diagnostics import percentile
from eval.metrics.refusal import analyze_refusal
from eval.metrics.retrieval import evaluate_retrieval
from eval.p1_retrieval_diagnostics import _rerank_raw, _span_rank
from eval.stats import MetricSamples, RatioPoint, paired_bootstrap
from eval.suites import load_suite, validate_suite
from workpilot_ai.gateway import ModelGateway

VARIANTS = ("ts_rank", "bm25")
METRIC_NAMES = (
    "span_recall_at_k",
    "gold_doc_recall_at_k",
    "ndcg_at_k",
    "max_doc_share_at_k",
)

CORPUS_SQL = """
SELECT c.id AS chunk_id, d.id AS document_id, c.version_id, v.version_no,
       d.title, d.source_uri, c.content, c.content_tokens, c.char_start, c.char_end,
       COALESCE(c.heading_path, ARRAY[]::text[]) AS heading_path,
       COALESCE(terms.term_frequencies, '{}'::jsonb) AS term_frequencies
FROM chunks c
JOIN document_versions v ON v.id=c.version_id
JOIN documents d ON d.id=v.document_id
LEFT JOIN LATERAL (
  SELECT jsonb_object_agg(grouped.lexeme, grouped.tf) AS term_frequencies
  FROM (
    SELECT combined.lexeme, sum(cardinality(combined.positions))::int AS tf
    FROM (
      SELECT u.lexeme, u.positions FROM unnest(c.tsv_en) AS u(lexeme, positions, weights)
      UNION ALL
      SELECT u.lexeme, u.positions FROM unnest(c.tsv_zh) AS u(lexeme, positions, weights)
    ) combined
    GROUP BY combined.lexeme
  ) grouped
) terms ON true
WHERE c.is_searchable=true AND c.strategy='heading'
  AND d.deleted_at IS NULL AND v.invalid_at IS NULL
ORDER BY c.id
"""

QUERY_TERMS_SQL = """
SELECT ARRAY(
         SELECT u.lexeme
         FROM unnest(to_tsvector('english', lexical_en_text(:query)))
              AS u(lexeme, positions, weights)
       ) AS en_terms,
       ARRAY(
         SELECT u.lexeme
         FROM unnest(to_tsvector('simple', lexical_zh_bigrams(:query)))
              AS u(lexeme, positions, weights)
       ) AS zh_terms
"""


@dataclass(frozen=True)
class Bm25Document:
    hit: DenseSearchHit
    length: int


class Bm25Index:
    def __init__(
        self,
        documents: list[Bm25Document],
        postings: dict[str, list[tuple[int, int]]],
        *,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> None:
        if not documents:
            raise ValueError("BM25 corpus 不能为空")
        self.documents = documents
        self.postings = postings
        self.k1 = k1
        self.b = b
        self.average_length = fmean(document.length for document in documents)

    def search(self, terms: list[str], *, top_k: int) -> list[DenseSearchHit]:
        scores: dict[int, float] = {}
        total = len(self.documents)
        for term in dict.fromkeys(terms):
            posting = self.postings.get(term)
            if not posting:
                continue
            document_frequency = len(posting)
            idf = math.log(
                1 + (total - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            for document_index, term_frequency in posting:
                document = self.documents[document_index]
                denominator = term_frequency + self.k1 * (
                    1 - self.b + self.b * document.length / self.average_length
                )
                scores[document_index] = scores.get(document_index, 0.0) + idf * (
                    term_frequency * (self.k1 + 1) / denominator
                )
        ranked = sorted(
            scores.items(),
            key=lambda item: (
                -item[1],
                self.documents[item[0]].length,
                str(self.documents[item[0]].hit.chunk_id),
            ),
        )[:top_k]
        return [
            _with_lexical_score(self.documents[index].hit, score)
            for index, score in ranked
        ]


@dataclass(frozen=True)
class ItemPool:
    dataset: str
    item: EvalItem
    dense: list[DenseSearchHit]
    ts_rank: list[DenseSearchHit]
    bm25: list[DenseSearchHit]
    candidates: dict[str, list[DenseSearchHit]]
    ideal: list[RetrievedChunk]


async def run_bm25_experiment(
    *,
    suite_path: Path,
    label: str,
    reranker_base_url: str,
    output_dir: Path,
    timeout_s: float = 120.0,
    settings: Settings | None = None,
) -> Path:
    settings = settings or Settings()
    suite = load_suite(suite_path)
    if "test" in suite.name.lower():
        raise ValueError("P1-G 禁止访问 test suite")
    suite_sha = hashlib.sha256(suite_path.read_bytes()).hexdigest()
    try:
        async with session_factory() as session:
            await validate_suite(session, suite)
            gateway = build_model_gateway(settings)
            try:
                bm25_index = await _load_bm25_index(session)
                pools = await _build_pools(
                    session,
                    gateway,
                    bm25_index=bm25_index,
                    suite=suite,
                    settings=settings,
                )
                if len(pools) != 70:
                    raise ValueError(f"P1-G 冻结轴漂移：预期 70，实际 {len(pools)}")
                pool_sha = _pool_fingerprint(pools)
                checkpoint_path = output_dir / f"{_slug(label)}.checkpoint.json"
                checkpoint = _load_checkpoint(
                    checkpoint_path, suite_sha=suite_sha, pool_sha=pool_sha
                )
                completed = checkpoint.get("items")
                items: dict[str, dict[str, object]] = (
                    completed if isinstance(completed, dict) else {}
                )
                async with httpx.AsyncClient(
                    base_url=reranker_base_url.rstrip("/"),
                    timeout=timeout_s,
                    trust_env=False,
                ) as client:
                    health_response = await client.get("/health")
                    health_response.raise_for_status()
                    health = health_response.json()
                    for pool in pools:
                        key = str(pool.item.id)
                        if key in items:
                            continue
                        items[key] = await _evaluate_item(
                            client, pool=pool, settings=settings
                        )
                        _write_checkpoint(
                            checkpoint_path,
                            {"items": items},
                            suite_sha=suite_sha,
                            pool_sha=pool_sha,
                        )
            finally:
                await gateway.aclose()

        ordered = [items[str(pool.item.id)] for pool in pools]
        payload: dict[str, object] = {
            "schema_version": "p1-bm25-replacement.v1",
            "label": label,
            "generated_at": datetime.now(UTC).isoformat(),
            "git_sha": _git_sha(),
            "suite": suite.name,
            "suite_sha256": suite_sha,
            "candidate_pool_sha256": pool_sha,
            "config": {
                "dense_k": 50,
                "lexical_k": 50,
                "rrf_k": settings.rrf_k,
                "rrf_candidate_k": 50,
                "candidate_chars": 1200,
                "server_tokens": 512,
                "top_k": 10,
                "bm25_k1": bm25_index.k1,
                "bm25_b": bm25_index.b,
                "bm25_corpus_chunks": len(bm25_index.documents),
            },
            "service_health": health,
            "summary": _summary(ordered, settings=settings),
            "items": ordered,
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{_slug(label)}"
        json_path = output_dir / f"{stem}.json"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (output_dir / f"{stem}.md").write_text(_render_markdown(payload), encoding="utf-8")
        return json_path
    finally:
        await close_database()


async def _load_bm25_index(session: AsyncSession) -> Bm25Index:
    rows = (await session.execute(text(CORPUS_SQL))).mappings().all()
    documents: list[Bm25Document] = []
    postings: dict[str, list[tuple[int, int]]] = {}
    for row in rows:
        frequencies = row["term_frequencies"]
        if not isinstance(frequencies, dict):
            raise TypeError("term_frequencies 必须是 JSON 对象")
        document_index = len(documents)
        hit = DenseSearchHit(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            version_id=row["version_id"],
            version_no=row["version_no"],
            title=row["title"],
            source_uri=row["source_uri"],
            content=row["content"],
            content_tokens=row["content_tokens"],
            char_start=row["char_start"],
            char_end=row["char_end"],
            score=0.0,
            heading_path=list(row["heading_path"]),
            blocks=[],
            strategy="heading",
        )
        length = sum(int(value) for value in frequencies.values())
        documents.append(Bm25Document(hit, max(length, 1)))
        for term, value in frequencies.items():
            postings.setdefault(str(term), []).append((document_index, int(value)))
    return Bm25Index(documents, postings)


async def _build_pools(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    bm25_index: Bm25Index,
    suite: Any,
    settings: Settings,
) -> list[ItemPool]:
    pools: list[ItemPool] = []
    for dataset in suite.datasets:
        _, items = await _load_items(session, dataset.name, origin=suite.origin)
        for item in items:
            embedding = (
                await gateway.embed([item.question], task_type="query_embedding")
            ).embeddings[0]
            dense = await _dense_search_by_vector(
                session, gateway, embedding=embedding, top_k=50, strategy="heading"
            )
            ts_rank = await lexical_search(
                session,
                query=item.question,
                top_k=50,
                mode="ts_rank",
                strategy="heading",
            )
            bm25_terms = await _query_terms(session, item.question)
            bm25 = bm25_index.search(bm25_terms, top_k=50)
            candidates = {
                "ts_rank": reciprocal_rank_fusion(
                    [dense, ts_rank], top_k=50, rrf_k=settings.rrf_k, strategy="heading"
                ),
                "bm25": reciprocal_rank_fusion(
                    [dense, bm25], top_k=50, rrf_k=settings.rrf_k, strategy="heading"
                ),
            }
            ideal: list[RetrievedChunk] = []
            if item.answerable:
                ideal = await _candidate_chunks(
                    session,
                    item.gold_spans,
                    embedding_model=gateway.embedding_model,
                    embedding_provider=gateway.embedding_provider,
                    embedding_revision=gateway.embedding_revision,
                    chunk_strategy="heading",
                    token_count_mode="stored",
                )
            pools.append(ItemPool(dataset.name, item, dense, ts_rank, bm25, candidates, ideal))
    return pools


async def _query_terms(session: AsyncSession, query: str) -> list[str]:
    row = (await session.execute(text(QUERY_TERMS_SQL), {"query": query})).mappings().one()
    return list(dict.fromkeys([*row["en_terms"], *row["zh_terms"]]))


async def _evaluate_item(
    client: httpx.AsyncClient, *, pool: ItemPool, settings: Settings
) -> dict[str, object]:
    variants: dict[str, object] = {}
    for name in VARIANTS:
        started = time.perf_counter()
        ranked = await _rerank_raw(
            client,
            query=pool.item.question,
            candidates=pool.candidates[name],
            model=settings.reranker_model,
            char_limit=1200,
            token_window=512,
            text_mode=settings.rerank_candidate_text_mode,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        top_hits = ranked[:10]
        signals = evaluate_refusal(
            top_hits,
            threshold=settings.refusal_threshold,
            margin_threshold=settings.refusal_margin_threshold,
        )
        metrics: dict[str, float] | None = None
        if pool.item.answerable:
            values = evaluate_retrieval(
                pool.item.gold_spans,
                [_retrieved_chunk(hit) for hit in ranked],
                pool.ideal,
                top_k=10,
                token_budget=6000,
                theta=0.5,
                alpha=0.5,
            ).to_dict()
            metrics = {metric: float(values[metric]) for metric in METRIC_NAMES}
        variants[name] = {
            "metrics": metrics,
            "latency_ms": latency_ms,
            "top_score": signals.top_score,
            "refused": signals.refusal_reason is not None,
            "span_ranks": [
                _span_rank(ranked, span, theta=0.5) for span in pool.item.gold_spans
            ],
        }
    ts_ids = {hit.chunk_id for hit in pool.ts_rank}
    bm_ids = {hit.chunk_id for hit in pool.bm25}
    union_ids = {hit.chunk_id for hit in rerank_candidate_union(pool.dense, pool.ts_rank, rrf_k=settings.rrf_k)}
    target_indexes = [
        index
        for index, span in enumerate(pool.item.gold_spans)
        if _span_rank(
            rerank_candidate_union(pool.dense, pool.ts_rank, rrf_k=settings.rrf_k),
            span,
            theta=0.5,
        )
        is None
    ]
    return {
        "item_id": str(pool.item.id),
        "dataset": pool.dataset,
        "category": pool.item.category,
        "answerable": pool.item.answerable,
        "cross_document": (
            pool.item.answerable
            and len({span.version_id for span in pool.item.gold_spans}) > 1
        ),
        "lexical_overlap": len(ts_ids & bm_ids) / len(ts_ids | bm_ids) if ts_ids | bm_ids else 1.0,
        "target_pool_outside_span_indexes": target_indexes,
        "ts_rank_union_candidate_count": len(union_ids),
        "variants": variants,
    }


def _summary(
    items: list[dict[str, object]], *, settings: Settings
) -> dict[str, object]:
    answerable = [item for item in items if bool(item["answerable"])]
    cross = [item for item in answerable if bool(item["cross_document"])]
    return {
        "all_answerable": _metric_slice(answerable),
        "cross_document_dev16": _metric_slice(cross),
        "by_category": {
            category: _metric_slice(
                [item for item in answerable if item["category"] == category]
            )
            for category in sorted({str(item["category"]) for item in answerable})
        },
        "refusal": _refusal_summary(items, threshold=settings.refusal_threshold),
        "lexical_top50_jaccard_mean": fmean(
            _number(item["lexical_overlap"]) for item in items
        ),
        "pool_outside": {
            "target_count": sum(len(_target_indexes(item)) for item in items),
            "rescued_by_bm25": _rescued_target_count(items, "bm25"),
        },
        "latency_ms": {name: _latency_summary(items, name) for name in VARIANTS},
    }


def _metric_slice(items: list[dict[str, object]]) -> dict[str, object]:
    if not items:
        return {"sample_size": 0}
    by_variant = {
        variant: {
            metric: fmean(_metric(item, variant, metric) for item in items)
            for metric in METRIC_NAMES
        }
        for variant in VARIANTS
    }
    samples = {
        metric: MetricSamples(
            tuple(RatioPoint(_metric(item, "ts_rank", metric), 1) for item in items),
            tuple(RatioPoint(_metric(item, "bm25", metric), 1) for item in items),
        )
        for metric in METRIC_NAMES
    }
    return {
        "sample_size": len(items),
        "by_variant": by_variant,
        "bm25_vs_ts_rank": {
            name: result.to_dict()
            for name, result in paired_bootstrap(
                samples, higher_is_better={"max_doc_share_at_k": False}
            ).items()
        },
    }


def _refusal_summary(items: list[dict[str, object]], *, threshold: float) -> dict[str, object]:
    return {
        name: analyze_refusal(
            [(_top_score(item, name), bool(item["answerable"])) for item in items],
            configured_threshold=threshold,
        ).to_dict()
        for name in VARIANTS
    }


def _rescued_target_count(items: list[dict[str, object]], variant: str) -> int:
    rescued = 0
    for item in items:
        ranks = _span_ranks(item, variant)
        for index in _target_indexes(item):
            rank = ranks[index]
            if isinstance(rank, int) and rank <= 10:
                rescued += 1
    return rescued


def _item_variant(item: dict[str, object], variant: str) -> dict[str, object]:
    variants = item["variants"]
    if not isinstance(variants, dict):
        raise TypeError("variants 结构非法")
    value = variants[variant]
    if not isinstance(value, dict):
        raise TypeError("variant 结构非法")
    return value


def _metric(item: dict[str, object], variant: str, metric: str) -> float:
    metrics = _item_variant(item, variant)["metrics"]
    if not isinstance(metrics, dict):
        raise TypeError("metrics 结构非法")
    return _number(metrics[metric])


def _top_score(item: dict[str, object], variant: str) -> float:
    return _number(_item_variant(item, variant)["top_score"])


def _span_ranks(item: dict[str, object], variant: str) -> list[object]:
    ranks = _item_variant(item, variant)["span_ranks"]
    if not isinstance(ranks, list):
        raise TypeError("span_ranks 结构非法")
    return ranks


def _target_indexes(item: dict[str, object]) -> list[int]:
    indexes = item["target_pool_outside_span_indexes"]
    if not isinstance(indexes, list) or not all(isinstance(index, int) for index in indexes):
        raise TypeError("target indexes 结构非法")
    return indexes


def _latency_summary(items: list[dict[str, object]], variant: str) -> dict[str, float]:
    values = sorted(
        _number(_item_variant(item, variant)["latency_ms"]) for item in items
    )
    p50 = percentile(values, 0.5)
    p95 = percentile(values, 0.95)
    assert p50 is not None and p95 is not None
    return {"mean": fmean(values), "p50": p50, "p95": p95, "max": max(values)}


def _with_lexical_score(hit: DenseSearchHit, score: float) -> DenseSearchHit:
    return replace(hit, score=score, lexical_score=score)


def _number(value: object) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise TypeError(f"预期数值，实际 {type(value).__name__}")
    return float(value)


def _pool_fingerprint(pools: list[ItemPool]) -> str:
    material = [
        {
            "item_id": str(pool.item.id),
            "dense": [str(hit.chunk_id) for hit in pool.dense],
            "ts_rank": [str(hit.chunk_id) for hit in pool.ts_rank],
            "bm25": [str(hit.chunk_id) for hit in pool.bm25],
        }
        for pool in pools
    ]
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _load_checkpoint(
    path: Path, *, suite_sha: str, pool_sha: str
) -> dict[str, object]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("P1-G checkpoint 必须是对象")
    if payload.get("suite_sha256") != suite_sha or payload.get("candidate_pool_sha256") != pool_sha:
        raise ValueError("P1-G checkpoint 与 suite/candidate pool 不一致")
    return payload


def _write_checkpoint(
    path: Path,
    payload: dict[str, object],
    *,
    suite_sha: str,
    pool_sha: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                **payload,
                "suite_sha256": suite_sha,
                "candidate_pool_sha256": pool_sha,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _render_markdown(payload: dict[str, object]) -> str:
    summary = payload["summary"]
    assert isinstance(summary, dict)
    answerable = summary["all_answerable"]
    cross = summary["cross_document_dev16"]
    pool_outside = summary["pool_outside"]
    assert isinstance(answerable, dict) and isinstance(cross, dict) and isinstance(pool_outside, dict)
    lines = [
        f"# P1-G BM25 替换 ts_rank · {payload['label']}",
        "",
        f"- suite: `{payload['suite']}`；dense50 + lexical50 → RRF50 → rerank；1200/512",
        f"- candidate pool SHA256: `{payload['candidate_pool_sha256']}`",
        f"- lexical Top-50 mean Jaccard: {_number(summary['lexical_top50_jaccard_mean']):.3f}",
        "",
        "| 切片 | 词法 | goldDocR | spanRec | maxShare | nDCG |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for slice_name, block in (("answerable dev", answerable), ("cross-doc dev16", cross)):
        variants = block["by_variant"]
        assert isinstance(variants, dict)
        for variant in VARIANTS:
            row = variants[variant]
            assert isinstance(row, dict)
            lines.append(
                f"| {slice_name} | `{variant}` | {_number(row['gold_doc_recall_at_k']):.3f} |"
                f" {_number(row['span_recall_at_k']):.3f} |"
                f" {_number(row['max_doc_share_at_k']):.3f} | {_number(row['ndcg_at_k']):.3f} |"
            )
    comparison = answerable["bm25_vs_ts_rank"]
    assert isinstance(comparison, dict)
    lines.extend(["", "## 完整 answerable dev 配对统计", ""])
    for metric in METRIC_NAMES:
        row = comparison[metric]
        assert isinstance(row, dict)
        lines.append(
            f"- `{metric}` Δ={_number(row['delta']):+.3f}，95% CI "
            f"[{_number(row['ci_low']):+.3f}, {_number(row['ci_high']):+.3f}]，"
            f"`{row['verdict']}`"
        )
    lines.extend(
        [
            "",
            f"- 原 ts_rank 池外 spans: {pool_outside['target_count']}；BM25 最终 Top-10 救回: {pool_outside['rescued_by_bm25']}",
            "",
        ]
    )
    return "\n".join(lines)


def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _slug(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in {"-", "_"} else "-" for char in value
    ).strip("-")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P1-G BM25 替换 ts_rank 单变量对照")
    parser.add_argument("--suite", type=Path, default=Path("eval/suites/m1-dev-70.json"))
    parser.add_argument("--label", required=True)
    parser.add_argument("--reranker-base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument("--output-dir", type=Path, default=Path("eval/outputs/p1-bm25"))
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    path = asyncio.run(
        run_bm25_experiment(
            suite_path=args.suite,
            label=args.label,
            reranker_base_url=args.reranker_base_url,
            output_dir=args.output_dir,
            timeout_s=args.timeout_s,
        )
    )
    print(f"报告已写入 {path}")


if __name__ == "__main__":
    main()
