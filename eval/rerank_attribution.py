"""P1 Step 1：重放旧的 ``RRF Top-50 -> rerank`` 链路并归因 gold chunk 漏失。

这不是质量跑批，不创建 eval_runs，也不修改生产配置。它只读 m1-dev-70 的 dev 跨文档题，
把每个 gold chunk 在 dense / lexical / RRF 截断 / cross-encoder 四阶段的状态写进
Git 忽略的报告，避免后续修复把不同失败形状混成一个“retrieval miss”。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import ModelGateway, build_model_gateway
from app.retrieval.dense import DenseSearchHit, dense_search
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.lexical import lexical_search
from app.retrieval.strategy import ChunkStrategy, validate_chunk_strategy
from app.services.reranker import rerank_candidates
from eval.dense_baseline import EvalItem, _load_items
from eval.mapping import GoldSpan
from eval.suites import load_suite, validate_suite

ATTRIBUTION_STATUSES = (
    "survives_top_k",
    "rerank_demoted",
    "rrf_truncated",
    "pool_outside",
)


@dataclass(frozen=True)
class GoldChunkAttribution:
    item_id: str
    dataset: str
    category: str
    gold_chunk_id: str
    version_id: str
    char_start: int
    char_end: int
    dense_rank: int | None
    lexical_rank: int | None
    rrf_rank: int | None
    rerank_rank: int | None
    status: str


def classify_attribution(
    *,
    dense_rank: int | None,
    lexical_rank: int | None,
    rrf_rank: int | None,
    rerank_rank: int | None,
    final_top_k: int,
) -> str:
    """把旧链路的单个 gold chunk 映射为互斥、可行动的失败状态。"""
    if dense_rank is None and lexical_rank is None:
        return "pool_outside"
    if rrf_rank is None:
        return "rrf_truncated"
    if rerank_rank is None or rerank_rank > final_top_k:
        return "rerank_demoted"
    return "survives_top_k"


async def run_rerank_attribution(
    *,
    suite_path: Path,
    label: str,
    per_arm_k: int,
    rrf_candidate_k: int,
    final_top_k: int,
    theta: float,
    output_dir: Path,
    settings: Settings | None = None,
) -> Path:
    """按生产旧配置重放 16 条 dev 跨文档题，rerank 不可用则失败而非静默改口径。"""
    if not 1 <= per_arm_k <= 100:
        raise ValueError("per_arm_k 必须位于 1 到 100")
    if not 1 <= rrf_candidate_k <= 50:
        raise ValueError("rrf_candidate_k 必须位于 1 到 50")
    if not 1 <= final_top_k <= rrf_candidate_k:
        raise ValueError("final_top_k 必须不大于 rrf_candidate_k")
    if not 0 < theta <= 1:
        raise ValueError("theta 必须位于 (0,1]")

    suite = load_suite(suite_path)
    if "test" in suite.name.lower():
        raise ValueError("归因回放禁止访问 test suite")
    settings = settings or Settings()
    strategy = validate_chunk_strategy("heading")
    rows: list[GoldChunkAttribution] = []

    async with session_factory() as session:
        await validate_suite(session, suite)
        gateway = build_model_gateway(settings, audit_sink=SqlLlmCallAudit(session))
        try:
            for dataset in suite.datasets:
                _, items = await _load_items(session, dataset.name, origin=suite.origin)
                for item in items:
                    if not _is_cross_document(item):
                        continue
                    rows.extend(
                        await _attribute_item(
                            session,
                            gateway,
                            item=item,
                            dataset=dataset.name,
                            per_arm_k=per_arm_k,
                            rrf_candidate_k=rrf_candidate_k,
                            final_top_k=final_top_k,
                            theta=theta,
                            settings=settings,
                            strategy=strategy,
                        )
                    )
        finally:
            await gateway.aclose()

    cross_document_items = len({row.item_id for row in rows})
    if cross_document_items != 16:
        raise ValueError(
            "P1 归因集合漂移：m1-dev-70 的 dev 跨文档题必须恰好为 16 条，"
            f"实际 {cross_document_items}"
        )
    status_counts = Counter(row.status for row in rows)
    payload: dict[str, object] = {
        "schema_version": "rerank-attribution.v1",
        "label": label,
        "generated_at": datetime.now(UTC).isoformat(),
        "git_sha": _git_sha(),
        "suite": suite.name,
        "suite_sha256": _sha256(suite_path),
        "cross_document_item_count": cross_document_items,
        "gold_chunk_count": len(rows),
        "config": {
            "chunk_strategy": strategy,
            "per_arm_k": per_arm_k,
            "rrf_candidate_k": rrf_candidate_k,
            "final_top_k": final_top_k,
            "theta": theta,
            "lexical_mode": settings.lexical_mode,
            "rrf_k": settings.rrf_k,
            "reranker_model": settings.reranker_model,
            "rerank_max_candidate_chars": settings.rerank_max_candidate_chars,
            "pipeline": "dense-per-arm + lexical-per-arm -> RRF top-50 -> cross-encoder",
        },
        "status_counts": {name: status_counts.get(name, 0) for name in ATTRIBUTION_STATUSES},
        "items": [asdict(row) for row in rows],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{_slug(label)}"
    json_path = output_dir / f"{stem}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / f"{stem}.md").write_text(_render_markdown(payload), encoding="utf-8")
    await close_database()
    return json_path


async def _attribute_item(
    session: AsyncSession,
    gateway: ModelGateway,
    *,
    item: EvalItem,
    dataset: str,
    per_arm_k: int,
    rrf_candidate_k: int,
    final_top_k: int,
    theta: float,
    settings: Settings,
    strategy: ChunkStrategy,
) -> list[GoldChunkAttribution]:
    dense_hits = await dense_search(
        session, gateway, query=item.question, top_k=per_arm_k, strategy=strategy
    )
    lexical_hits = await lexical_search(
        session,
        query=item.question,
        top_k=per_arm_k,
        mode=settings.lexical_mode,
        strategy=strategy,
    )
    rrf_hits = reciprocal_rank_fusion(
        [dense_hits, lexical_hits],
        top_k=rrf_candidate_k,
        rrf_k=settings.rrf_k,
        strategy=strategy,
    )
    reranked = await rerank_candidates(
        query=item.question,
        candidates=rrf_hits,
        top_k=len(rrf_hits),
        base_url=settings.reranker_base_url,
        model=settings.reranker_model,
        timeout_s=settings.reranker_timeout_s,
        max_candidate_chars=settings.rerank_max_candidate_chars,
        candidate_text_mode=settings.rerank_candidate_text_mode,
        strategy=strategy,
    )
    if not reranked.applied:
        raise RuntimeError(f"rerank 回放失败: item={item.id}, reason={reranked.reason}")

    dense_ranks = _rank_map(dense_hits)
    lexical_ranks = _rank_map(lexical_hits)
    rrf_ranks = _rank_map(rrf_hits)
    rerank_ranks = _rank_map(reranked.hits)
    gold_chunks = await _gold_chunks(session, item.gold_spans, theta=theta, strategy=strategy)
    if not gold_chunks:
        raise ValueError(f"跨文档题没有可检索 gold chunk: item={item.id}")
    return [
        GoldChunkAttribution(
            item_id=str(item.id),
            dataset=dataset,
            category=item.category,
            gold_chunk_id=str(chunk_id),
            version_id=str(version_id),
            char_start=char_start,
            char_end=char_end,
            dense_rank=dense_ranks.get(chunk_id),
            lexical_rank=lexical_ranks.get(chunk_id),
            rrf_rank=rrf_ranks.get(chunk_id),
            rerank_rank=rerank_ranks.get(chunk_id),
            status=classify_attribution(
                dense_rank=dense_ranks.get(chunk_id),
                lexical_rank=lexical_ranks.get(chunk_id),
                rrf_rank=rrf_ranks.get(chunk_id),
                rerank_rank=rerank_ranks.get(chunk_id),
                final_top_k=final_top_k,
            ),
        )
        for chunk_id, version_id, char_start, char_end in gold_chunks
    ]


async def _gold_chunks(
    session: AsyncSession,
    spans: list[GoldSpan],
    *,
    theta: float,
    strategy: ChunkStrategy,
) -> list[tuple[UUID, UUID, int, int]]:
    version_ids = list({span.version_id for span in spans})
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT id, version_id, char_start, char_end
                    FROM chunks
                    WHERE is_searchable=true
                      AND strategy=:strategy
                      AND version_id=ANY(:version_ids)
                    """
                ),
                {"strategy": strategy, "version_ids": version_ids},
            )
        )
        .mappings()
        .all()
    )
    matched: dict[UUID, tuple[UUID, int, int]] = {}
    for row in rows:
        version_id = row["version_id"]
        start, end = int(row["char_start"]), int(row["char_end"])
        for span in spans:
            if span.version_id != version_id:
                continue
            overlap = max(0, min(end, span.char_end) - max(start, span.char_start))
            if overlap / (span.char_end - span.char_start) >= theta:
                matched[row["id"]] = (version_id, start, end)
                break
    return [(chunk_id, *value) for chunk_id, value in sorted(matched.items(), key=lambda item: str(item[0]))]


def _is_cross_document(item: EvalItem) -> bool:
    return item.answerable and len({span.version_id for span in item.gold_spans}) > 1


def _rank_map(hits: list[DenseSearchHit]) -> dict[UUID, int]:
    return {hit.chunk_id: rank for rank, hit in enumerate(hits, start=1)}


def _render_markdown(payload: dict[str, object]) -> str:
    config = payload["config"]
    counts = payload["status_counts"]
    rows = payload["items"]
    assert isinstance(config, dict) and isinstance(counts, dict) and isinstance(rows, list)
    lines = [
        f"# P1 rerank 候选归因回放 · {payload['label']}",
        "",
        f"- suite: `{payload['suite']}`，跨文档题: {payload['cross_document_item_count']}，gold chunk: {payload['gold_chunk_count']}",
        f"- 链路: {config['pipeline']}",
        f"- 每臂: {config['per_arm_k']}，RRF 截断: {config['rrf_candidate_k']}，最终 Top-K: {config['final_top_k']}",
        "",
        "| 状态 | gold chunk 数 | 解释 |",
        "|---|---:|---|",
        f"| `rrf_truncated` | {counts['rrf_truncated']} | 单臂进池，但 RRF Top-50 前被截掉 |",
        f"| `rerank_demoted` | {counts['rerank_demoted']} | RRF 候选存活，但 cross-encoder 排到最终 Top-K 外 |",
        f"| `pool_outside` | {counts['pool_outside']} | dense/lexical 两臂都未进池 |",
        f"| `survives_top_k` | {counts['survives_top_k']} | 最终 Top-K 仍可见 |",
        "",
        "| item | dataset | gold chunk | dense | lexical | RRF | rerank | 状态 |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        assert isinstance(row, dict)
        lines.append(
            f"| `{str(row['item_id'])[:8]}` | `{row['dataset']}` | `{str(row['gold_chunk_id'])[:8]}` |"
            f" {_display_rank(row, 'dense_rank')} | {_display_rank(row, 'lexical_rank')} |"
            f" {_display_rank(row, 'rrf_rank')} | {_display_rank(row, 'rerank_rank')} |"
            f" `{row['status']}` |"
        )
    return "\n".join(lines) + "\n"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _display_rank(row: dict[str, object], name: str) -> str:
    value = row[name]
    return "—" if value is None else str(value)


def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value).strip("-")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P1 rerank 候选归因回放")
    parser.add_argument("--suite", type=Path, default=Path("eval/suites/m1-dev-70.json"))
    parser.add_argument("--label", required=True)
    parser.add_argument("--per-arm-k", type=int, default=50)
    parser.add_argument("--rrf-candidate-k", type=int, default=50)
    parser.add_argument("--final-top-k", type=int, default=10)
    parser.add_argument("--theta", type=float, default=0.5)
    parser.add_argument("--output-dir", type=Path, default=Path("eval/outputs/rerank-attribution"))
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    path = asyncio.run(
        run_rerank_attribution(
            suite_path=args.suite,
            label=args.label,
            per_arm_k=args.per_arm_k,
            rrf_candidate_k=args.rrf_candidate_k,
            final_top_k=args.final_top_k,
            theta=args.theta,
            output_dir=args.output_dir,
        )
    )
    print(f"报告已写入 {path}")


if __name__ == "__main__":
    main()
