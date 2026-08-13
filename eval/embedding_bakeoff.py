import argparse
import asyncio
import hashlib
import json
import math
import statistics
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class Candidate:
    name: str
    model: str
    revision: str
    base_url: str
    expected_dimensions: int
    query_prefix: str = ""


@dataclass(frozen=True)
class CorpusItem:
    id: str
    title: str
    text: str
    context_tokens: int


@dataclass(frozen=True)
class QueryItem:
    id: str
    query: str
    relevant_ids: list[str]
    answerable: bool
    category: str


@dataclass(frozen=True)
class RankedItem:
    id: str
    score: float
    context_tokens: int


class EmbeddingEndpoint:
    def __init__(self, candidate: Candidate, *, timeout_s: float) -> None:
        self.candidate = candidate
        self.client = httpx.AsyncClient(
            base_url=candidate.base_url.rstrip("/") + "/",
            timeout=timeout_s,
            trust_env=False,
            headers={"Authorization": "Bearer local"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def verify(self) -> None:
        response = await self.client.get("models")
        response.raise_for_status()
        model_ids = {str(item["id"]) for item in response.json().get("data", [])}
        if self.candidate.model not in model_ids:
            raise RuntimeError(
                f"{self.candidate.name}: /v1/models 未返回 {self.candidate.model}; "
                f"实际为 {sorted(model_ids)}"
            )

    async def embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        inputs = [self.candidate.query_prefix + value if query else value for value in texts]
        response = await self.client.post(
            "embeddings",
            json={"model": self.candidate.model, "input": inputs},
        )
        response.raise_for_status()
        rows = sorted(response.json().get("data", []), key=lambda item: int(item["index"]))
        embeddings = [_normalize([float(value) for value in row["embedding"]]) for row in rows]
        if len(embeddings) != len(texts):
            raise RuntimeError(f"{self.candidate.name}: embedding 数量与输入不一致")
        dimensions = {len(vector) for vector in embeddings}
        if dimensions != {self.candidate.expected_dimensions}:
            raise RuntimeError(
                f"{self.candidate.name}: 期望 {self.candidate.expected_dimensions} 维, "
                f"实际为 {sorted(dimensions)}"
            )
        return embeddings


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        raise RuntimeError("embedding 返回了零向量")
    return [value / norm for value in vector]


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError(f"{path}:{line_no}: JSON 无效") from error
    return records


def load_corpus(path: Path) -> list[CorpusItem]:
    items = [CorpusItem(**record) for record in _load_jsonl(path)]
    ids = [item.id for item in items]
    if not items or len(ids) != len(set(ids)):
        raise ValueError("corpus 必须非空且 id 唯一")
    return items


def load_queries(path: Path, corpus_ids: set[str]) -> list[QueryItem]:
    queries = [QueryItem(**record) for record in _load_jsonl(path)]
    ids = [item.id for item in queries]
    if not queries or len(ids) != len(set(ids)):
        raise ValueError("queries 必须非空且 id 唯一")
    for item in queries:
        if item.answerable != bool(item.relevant_ids):
            raise ValueError(f"{item.id}: answerable 与 relevant_ids 不一致")
        unknown = set(item.relevant_ids) - corpus_ids
        if unknown:
            raise ValueError(f"{item.id}: 引用了不存在的 corpus id {sorted(unknown)}")
    return queries


def rank_corpus(
    query_vector: list[float],
    corpus: list[CorpusItem],
    corpus_vectors: list[list[float]],
) -> list[RankedItem]:
    ranked = [
        RankedItem(
            id=item.id,
            score=sum(left * right for left, right in zip(query_vector, vector, strict=True)),
            context_tokens=item.context_tokens,
        )
        for item, vector in zip(corpus, corpus_vectors, strict=True)
    ]
    return sorted(ranked, key=lambda item: (-item.score, item.id))


def retrieval_metrics(
    queries: list[QueryItem],
    rankings: dict[str, list[RankedItem]],
    *,
    top_k: int,
    token_budget: int,
) -> dict[str, float]:
    answerable = [item for item in queries if item.answerable]
    recalls: list[float] = []
    budget_recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    for query in answerable:
        relevant = set(query.relevant_ids)
        ranked = rankings[query.id]
        top = ranked[:top_k]
        recalls.append(len({item.id for item in top} & relevant) / len(relevant))
        first_rank = next(
            (index for index, item in enumerate(ranked, start=1) if item.id in relevant), None
        )
        reciprocal_ranks.append(0.0 if first_rank is None else 1.0 / first_rank)
        dcg = sum(
            1.0 / math.log2(index + 1)
            for index, item in enumerate(top, start=1)
            if item.id in relevant
        )
        ideal_count = min(len(relevant), top_k)
        idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_count + 1))
        ndcgs.append(dcg / idcg if idcg else 0.0)

        spent = 0
        budget_ids: set[str] = set()
        for item in ranked:
            if spent + item.context_tokens > token_budget:
                break
            spent += item.context_tokens
            budget_ids.add(item.id)
            if spent >= token_budget:
                break
        budget_recalls.append(len(budget_ids & relevant) / len(relevant))

    return {
        f"span_recall_at_{top_k}": statistics.fmean(recalls),
        f"ndcg_at_{top_k}": statistics.fmean(ndcgs),
        "mrr": statistics.fmean(reciprocal_ranks),
        f"span_recall_at_{token_budget}_estimated_tokens": statistics.fmean(budget_recalls),
    }


def refusal_metrics(
    queries: list[QueryItem], rankings: dict[str, list[RankedItem]]
) -> dict[str, Any]:
    scores = [(item.answerable, rankings[item.id][0].score) for item in queries]
    positive = [score for answerable, score in scores if answerable]
    negative = [score for answerable, score in scores if not answerable]
    pairwise = [
        1.0 if pos > neg else 0.5 if pos == neg else 0.0 for pos in positive for neg in negative
    ]
    auroc = statistics.fmean(pairwise) if pairwise else 0.0

    thresholds = sorted({score for _, score in scores} | {-1.0, 1.0 + 1e-9})
    candidates: list[dict[str, float]] = []
    for threshold in thresholds:
        true_positive = sum(answerable and score >= threshold for answerable, score in scores)
        false_positive = sum(not answerable and score >= threshold for answerable, score in scores)
        false_negative = sum(answerable and score < threshold for answerable, score in scores)
        true_negative = sum(not answerable and score < threshold for answerable, score in scores)
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 0
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 0
        )
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
        candidates.append(
            {
                "threshold": threshold,
                "f1": f1,
                "false_accept_rate": false_positive / (false_positive + true_negative),
                "false_reject_rate": false_negative / (true_positive + false_negative),
            }
        )
    best = max(
        candidates,
        key=lambda item: (item["f1"], -item["false_accept_rate"], item["threshold"]),
    )
    return {
        "answerable_top_score_mean": statistics.fmean(positive),
        "unanswerable_top_score_mean": statistics.fmean(negative),
        "auroc": auroc,
        "best_threshold": best["threshold"],
        "best_f1": best["f1"],
        "false_accept_rate": best["false_accept_rate"],
        "false_reject_rate": best["false_reject_rate"],
        "answerable_top_scores": positive,
        "unanswerable_top_scores": negative,
    }


async def evaluate_candidate(
    candidate: Candidate,
    corpus: list[CorpusItem],
    queries: list[QueryItem],
    *,
    batch_size: int,
    top_k: int,
    token_budget: int,
    timeout_s: float,
) -> dict[str, Any]:
    endpoint = EmbeddingEndpoint(candidate, timeout_s=timeout_s)
    try:
        await endpoint.verify()
        started = time.perf_counter()
        corpus_vectors: list[list[float]] = []
        for index in range(0, len(corpus), batch_size):
            corpus_vectors.extend(
                await endpoint.embed(
                    [item.text for item in corpus[index : index + batch_size]], query=False
                )
            )
        corpus_seconds = time.perf_counter() - started

        query_vectors: list[list[float]] = []
        latencies_ms: list[float] = []
        for query in queries:
            query_started = time.perf_counter()
            query_vectors.extend(await endpoint.embed([query.query], query=True))
            latencies_ms.append((time.perf_counter() - query_started) * 1000)

        rankings = {
            query.id: rank_corpus(vector, corpus, corpus_vectors)
            for query, vector in zip(queries, query_vectors, strict=True)
        }
        return {
            "candidate": asdict(candidate),
            "corpus": {
                "items": len(corpus),
                "seconds": corpus_seconds,
                "items_per_second": len(corpus) / corpus_seconds,
            },
            "query_latency_ms": {
                "p50": statistics.median(latencies_ms),
                "p95": _percentile(latencies_ms, 0.95),
                "mean": statistics.fmean(latencies_ms),
            },
            "retrieval": retrieval_metrics(
                queries, rankings, top_k=top_k, token_budget=token_budget
            ),
            "refusal": refusal_metrics(queries, rankings),
            "rankings": {
                query.id: [asdict(item) for item in rankings[query.id][:top_k]] for query in queries
            },
        }
    finally:
        await endpoint.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or "unknown"


def load_config(path: Path) -> tuple[list[Candidate], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [Candidate(**item) for item in payload["candidates"]], payload


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# E0 · Embedding 模型对照",
        "",
        f"- 时间: {report['created_at']}",
        f"- Git SHA: `{report['git_sha']}`",
        f"- 数据集: {report['dataset']['queries']} queries / {report['dataset']['corpus']} blocks",
        "- 注意: smoke fixture 只能验证流程与早期方向, 不能替代真实私人语料选型。",
        "",
        "| Candidate | Recall@K | nDCG@K | MRR | Budget Recall | AUROC | "
        "Threshold | FAR | FRR | Query p95 ms | Corpus item/s |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in report["results"]:
        retrieval = result["retrieval"]
        refusal = result["refusal"]
        recall_key = next(
            key for key in retrieval if key.startswith("span_recall_at_") and "estimated" not in key
        )
        budget_key = next(key for key in retrieval if key.endswith("_estimated_tokens"))
        lines.append(
            "| {name} | {recall:.3f} | {ndcg:.3f} | {mrr:.3f} | {budget:.3f} | "
            "{auroc:.3f} | {threshold:.4f} | {far:.3f} | {frr:.3f} | "
            "{p95:.1f} | {ips:.2f} |".format(
                name=result["candidate"]["name"],
                recall=retrieval[recall_key],
                ndcg=next(value for key, value in retrieval.items() if key.startswith("ndcg_at_")),
                mrr=retrieval["mrr"],
                budget=retrieval[budget_key],
                auroc=refusal["auroc"],
                threshold=refusal["best_threshold"],
                far=refusal["false_accept_rate"],
                frr=refusal["false_reject_rate"],
                p95=result["query_latency_ms"]["p95"],
                ips=result["corpus"]["items_per_second"],
            )
        )
    lines.extend(
        [
            "",
            "## 解释",
            "",
            "- 主指标是 span recall 与固定估算 token budget 下的 recall。",
            "- 阈值按各模型自己的 top-score 分布计算, 不能跨模型复用。",
            "- 若质量差异小于 1-2 个百分点, 优先选择延迟更低、部署更稳定的候选。",
            "",
        ]
    )
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> Path:
    config_path = Path(args.config)
    corpus_path = Path(args.corpus)
    queries_path = Path(args.queries)
    candidates, config = load_config(config_path)
    corpus = load_corpus(corpus_path)
    queries = load_queries(queries_path, {item.id for item in corpus})

    results = []
    for candidate in candidates:
        print(f"evaluating {candidate.name} ({candidate.model})...", flush=True)
        results.append(
            await evaluate_candidate(
                candidate,
                corpus,
                queries,
                batch_size=int(config["batch_size"]),
                top_k=int(config["top_k"]),
                token_budget=int(config["estimated_token_budget"]),
                timeout_s=float(config["timeout_s"]),
            )
        )

    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "git_sha": _git_sha(),
        "config_sha256": _sha256(config_path),
        "dataset": {
            "corpus": len(corpus),
            "queries": len(queries),
            "answerable": sum(item.answerable for item in queries),
            "unanswerable": sum(not item.answerable for item in queries),
            "corpus_sha256": _sha256(corpus_path),
            "queries_sha256": _sha256(queries_path),
        },
        "results": results,
    }
    return await asyncio.to_thread(_write_report, report, args.output_dir)


def _write_report(report: dict[str, Any], output_dir_value: str) -> Path:
    output_dir = Path(output_dir_value)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = output_dir / f"{stamp}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    json_path.with_suffix(".md").write_text(render_markdown(report), encoding="utf-8")
    return json_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对照 OpenAI-compatible embedding 模型")
    parser.add_argument("--config", default="config/embedding-bakeoff.json")
    parser.add_argument("--corpus", default="eval/datasets/embedding-smoke/corpus.jsonl")
    parser.add_argument("--queries", default="eval/datasets/embedding-smoke/queries.jsonl")
    parser.add_argument("--output-dir", default="eval/outputs/embedding-bakeoff")
    return parser.parse_args()


if __name__ == "__main__":
    output = asyncio.run(run(parse_args()))
    print(f"report: {output}")
