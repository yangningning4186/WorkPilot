import argparse
import asyncio
import json
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import ModelGateway, build_model_gateway
from app.retrieval.dense import DenseSearchHit, dense_search
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.lexical import lexical_search
from app.services.reranker import rerank_candidates
from eval.metrics.diagnostics import percentile


@dataclass(frozen=True)
class LatencySample:
    question: str
    candidate_count: int
    latency_ms: float


async def run_reranker_latency(
    *,
    dataset_name: str,
    label: str,
    candidate_counts: list[int],
    top_k: int,
    repeat: int,
    warmup: int,
    output_dir: Path,
    settings: Settings | None = None,
) -> Path:
    settings = settings or Settings()
    if repeat < 1:
        raise ValueError("repeat 必须大于 0")
    if warmup < 0:
        raise ValueError("warmup 不能为负")
    if not candidate_counts:
        raise ValueError("candidate_counts 不能为空")
    if min(candidate_counts) <= top_k:
        raise ValueError("candidate_counts 必须全部大于 top_k")

    max_candidates = max(candidate_counts)

    async with session_factory() as session:
        gateway = build_model_gateway(settings, audit_sink=SqlLlmCallAudit(session))
        questions = await _load_questions(session, dataset_name)
        if not questions:
            raise RuntimeError(f"数据集 {dataset_name} 没有样本")
        # 候选检索不计入精排延迟, 先一次性构造再复用, 避免把 dense/lexical 的耗时算进去。
        candidates = [
            await _build_candidates(
                session,
                gateway,
                query=question,
                top_k=max_candidates,
                settings=settings,
            )
            for question in questions
        ]

    samples: list[LatencySample] = []
    async with httpx.AsyncClient(
        base_url=settings.reranker_base_url.rstrip("/"),
        timeout=settings.reranker_timeout_s,
        trust_env=False,
    ) as client:
        health = (await client.get("/health")).json()
        for _ in range(warmup):
            await _timed_rerank(
                client,
                query=questions[0],
                candidates=candidates[0][:max_candidates],
                top_k=top_k,
                settings=settings,
            )
        for candidate_count in candidate_counts:
            for question, hits in zip(questions, candidates, strict=True):
                batch = hits[:candidate_count]
                if len(batch) <= top_k:
                    continue
                for _ in range(repeat):
                    latency_ms = await _timed_rerank(
                        client,
                        query=question,
                        candidates=batch,
                        top_k=top_k,
                        settings=settings,
                    )
                    samples.append(LatencySample(question, len(batch), latency_ms))

    payload = {
        "label": label,
        "dataset": dataset_name,
        "generated_at": datetime.now(UTC).isoformat(),
        "git_sha": _git_sha(),
        "config": {
            "reranker_base_url": settings.reranker_base_url,
            "rerank_max_candidate_chars": settings.rerank_max_candidate_chars,
            "top_k": top_k,
            "repeat": repeat,
            "warmup": warmup,
            "question_count": len(questions),
        },
        "service_health": health,
        "by_candidate_count": _summarize(samples),
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{_slug(label)}"
    json_path = output_dir / f"{stem}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / f"{stem}.md").write_text(_render_markdown(payload), encoding="utf-8")
    await close_database()
    return json_path


async def _timed_rerank(
    client: httpx.AsyncClient,
    *,
    query: str,
    candidates: list[DenseSearchHit],
    top_k: int,
    settings: Settings,
) -> float:
    started = time.perf_counter()
    result = await rerank_candidates(
        query=query,
        candidates=candidates,
        top_k=top_k,
        base_url=settings.reranker_base_url,
        model=settings.reranker_model,
        timeout_s=settings.reranker_timeout_s,
        max_candidate_chars=settings.rerank_max_candidate_chars,
        client=client,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    if not result.applied:
        raise RuntimeError(result.reason)
    return elapsed_ms


async def _build_candidates(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    query: str,
    top_k: int,
    settings: Settings,
) -> list[DenseSearchHit]:
    dense_hits = await dense_search(session, gateway, query=query, top_k=top_k)
    lexical_hits = await lexical_search(
        session, query=query, top_k=top_k, mode=settings.lexical_mode
    )
    return reciprocal_rank_fusion([dense_hits, lexical_hits], top_k=top_k, rrf_k=settings.rrf_k)


async def _load_questions(session: AsyncSession, dataset_name: str) -> list[str]:
    rows = (
        await session.execute(
            text(
                """
                SELECT i.question
                FROM eval_items i
                JOIN eval_datasets d ON d.id = i.dataset_id
                WHERE d.name = :name
                ORDER BY i.id
                """
            ),
            {"name": dataset_name},
        )
    ).all()
    return [str(row[0]) for row in rows]


def _summarize(samples: list[LatencySample]) -> list[dict[str, object]]:
    summary: list[dict[str, object]] = []
    for candidate_count in sorted({sample.candidate_count for sample in samples}):
        latencies = sorted(
            sample.latency_ms for sample in samples if sample.candidate_count == candidate_count
        )
        summary.append(
            {
                "candidate_count": candidate_count,
                "samples": len(latencies),
                "mean_ms": fmean(latencies),
                "p50_ms": percentile(latencies, 0.5),
                "p95_ms": percentile(latencies, 0.95),
                "max_ms": latencies[-1],
                "ms_per_candidate": fmean(latencies) / candidate_count,
            }
        )
    return summary


def _render_markdown(payload: dict[str, object]) -> str:
    config = payload["config"]
    health = payload["service_health"]
    assert isinstance(config, dict) and isinstance(health, dict)
    lines = [
        f"# 本地 cross-encoder 精排延迟 · {payload['label']}",
        "",
        f"- 数据集: `{payload['dataset']}`",
        f"- Git: `{payload['git_sha']}`",
        (
            f"- 模型: `{health.get('model')}` / device `{health.get('device')}`"
            f" / dtype `{health.get('dtype')}` / batch `{health.get('batch_size')}`"
            f" / max_length `{health.get('max_length')}`"
        ),
        (
            f"- 问题数: {config['question_count']}, 每组重复 {config['repeat']} 次,"
            f" warmup {config['warmup']} 次, Top-K={config['top_k']}"
        ),
        f"- 候选正文截断: {config['rerank_max_candidate_chars']} 字符",
        "",
        "| 候选数 | 样本 | mean | p50 | p95 | max | 每候选 |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    rows = payload["by_candidate_count"]
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, dict)
        lines.append(
            f"| {row['candidate_count']} | {row['samples']} |"
            f" {row['mean_ms']:.1f}ms | {row['p50_ms']:.1f}ms |"
            f" {row['p95_ms']:.1f}ms | {row['max_ms']:.1f}ms |"
            f" {row['ms_per_candidate']:.1f}ms |"
        )
    lines.append("")
    lines.append("延迟只覆盖 `/v1/rerank` 往返, 不含 dense/lexical 检索与答案生成。")
    return "\n".join(lines) + "\n"


def _git_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _slug(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in {"-", "_"} else "-" for character in value
    ).strip("-")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="测量本地 cross-encoder 的精排延迟")
    parser.add_argument("--dataset", default="multihop-test-v1")
    parser.add_argument("--label", required=True)
    parser.add_argument("--candidate-counts", default="10,25,50")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--output-dir", type=Path, default=Path("eval/outputs/reranker-latency"))
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    candidate_counts = [int(part) for part in args.candidate_counts.split(",") if part.strip()]
    path = asyncio.run(
        run_reranker_latency(
            dataset_name=args.dataset,
            label=args.label,
            candidate_counts=candidate_counts,
            top_k=args.top_k,
            repeat=args.repeat,
            warmup=args.warmup,
            output_dir=args.output_dir,
        )
    )
    print(f"报告已写入 {path}")


if __name__ == "__main__":
    main()
