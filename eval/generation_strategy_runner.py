"""E1 四套 chunk strategy 的端到端生成跑批入口。

检索轨(`eval.chunk_strategy_runner`)证明的是"哪套分块更容易把 gold span 捞回来";
这里证明的是"捞回来之后, 答案与引用是不是真的更好"。两轨必须跑在同一批样本、
同一份 gold 标注、同一个模型与同一条 prompt 上, 否则端到端差异无法归因到分块。

    PYTHONPATH=backend backend/.venv/bin/python -m eval.generation_strategy_runner \\
      --manifest eval/outputs/chunk-strategies/<batch>/manifest.json \\
      --label e1-generation-core-dev

**fail-closed**: 检索 manifest 缺策略、语料指纹漂移、数据集或标注指纹与检索轨对不上、
四个 run 的受控配置不完全一致——任何一条不满足都直接中止, 不产出 manifest。
**幂等**: 同一份配置重复执行会复用已完成的 run, 不会重复烧钱重跑。
"""

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm_bootstrap import build_model_gateway
from app.rag.retrieval.strategy import CHUNK_STRATEGIES, ChunkStrategy
from eval.chunk_strategy_runner import ChunkCorpusNotReadyError, preflight_chunk_corpus
from eval.generation_baseline import (
    GENERATION_RETRIEVAL_STRATEGIES,
    prompt_fingerprint,
    run_generation_baseline,
)

# 这些 config 键在四个 run 之间必须逐字相同, 只有 chunk_strategy 及其元数据允许变。
# 少一条就意味着"单变量对照"这个前提没成立。
CONTROLLED_CONFIG_KEYS: tuple[str, ...] = (
    "dataset",
    "dataset_fingerprint",
    "annotation_fingerprint",
    "origin",
    "strategy",
    "top_k",
    "theta",
    "prompt_fingerprint",
    "refusal_threshold",
    "refusal_margin_threshold",
    "query_decomposition_enabled",
    "coverage_selection_enabled",
    "coverage_rank_cutoff",
    "rerank_enabled",
    "lexical_rrf_enabled",
    "embedding_model",
    "embedding_revision",
    "chat_model",
    "chat_provider",
    "evidence_gate_max_chars",
    "rerank_evidence_gate_max_chars",
    "evidence_gate_max_tokens",
    "rerank_candidate_k",
    "reranker_model",
    "rerank_candidate_text_mode",
    "lexical_mode",
    "rrf_k",
    "answer_max_evidence_chars",
    "answer_max_tokens",
)


class GenerationTrackNotReadyError(RuntimeError):
    """生成轨前置条件不满足。抛出即中止, 不产出任何 run 或 manifest。"""


@dataclass(frozen=True)
class RetrievalManifest:
    """检索轨 manifest 里生成轨必须继承的部分。"""

    path: Path
    dataset: str
    origin: str
    label: str
    dataset_fingerprint: str
    corpus_fingerprint: str
    retrieval_strategy: str
    top_k: int
    theta: float
    embedding_identity: dict[str, str]
    chunk_summaries: dict[str, dict[str, Any]]
    run_ids: dict[str, str]


@dataclass(frozen=True)
class GenerationBatchResult:
    manifest_path: Path
    run_ids: dict[ChunkStrategy, UUID]
    reused: dict[ChunkStrategy, bool]


def load_retrieval_manifest(path: Path) -> RetrievalManifest:
    """读取检索轨 manifest, 并核对它自己是完整的四策略批次。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise GenerationTrackNotReadyError(f"检索 manifest 根节点必须是对象: {path}")
    runs = payload.get("runs")
    if not isinstance(runs, dict) or set(runs) != set(CHUNK_STRATEGIES):
        raise GenerationTrackNotReadyError(
            "检索 manifest 必须恰好包含四套策略, 实际: "
            f"{sorted(runs) if isinstance(runs, dict) else runs}"
        )
    preflight = payload.get("preflight")
    if not isinstance(preflight, dict) or not isinstance(preflight.get("strategies"), list):
        raise GenerationTrackNotReadyError(f"检索 manifest 缺少 preflight.strategies: {path}")
    missing = [
        key
        for key in ("dataset", "origin", "dataset_fingerprint", "corpus_fingerprint")
        if not payload.get(key)
    ]
    if missing:
        raise GenerationTrackNotReadyError(f"检索 manifest 缺少字段 {missing}: {path}")
    identity = payload.get("embedding_identity")
    if not isinstance(identity, dict):
        raise GenerationTrackNotReadyError(f"检索 manifest 缺少 embedding_identity: {path}")
    summaries = {
        str(entry["strategy"]): entry
        for entry in preflight["strategies"]
        if isinstance(entry, dict) and entry.get("strategy")
    }
    if set(summaries) != set(CHUNK_STRATEGIES):
        raise GenerationTrackNotReadyError(
            f"检索 manifest 的 preflight 未覆盖四套策略: {sorted(summaries)}"
        )
    return RetrievalManifest(
        path=path,
        dataset=str(payload["dataset"]),
        origin=str(payload["origin"]),
        label=str(payload.get("label") or ""),
        dataset_fingerprint=str(payload["dataset_fingerprint"]),
        corpus_fingerprint=str(payload["corpus_fingerprint"]),
        retrieval_strategy=str(payload.get("retrieval_strategy") or "dense-only"),
        top_k=int(payload["top_k"]),
        theta=float(payload["theta"]),
        embedding_identity={str(k): str(v) for k, v in identity.items()},
        chunk_summaries=summaries,
        run_ids={
            strategy: str(entry["run_id"])
            for strategy, entry in runs.items()
            if isinstance(entry, dict) and entry.get("run_id")
        },
    )


async def run_generation_strategy_batch(
    *,
    manifest_path: Path,
    label: str,
    output_root: Path,
    top_k: int | None = None,
    theta: float | None = None,
    retrieval_strategy: str | None = None,
    rerank_candidate_text_mode: str | None = None,
    lexical_mode: str | None = None,
    reuse_completed: bool = True,
    settings: Settings | None = None,
) -> GenerationBatchResult:
    settings = settings or Settings()
    retrieval = load_retrieval_manifest(manifest_path)
    strategy = retrieval_strategy or retrieval.retrieval_strategy
    if strategy not in GENERATION_RETRIEVAL_STRATEGIES:
        raise GenerationTrackNotReadyError(
            f"生成轨无法复现检索轨的链路 {strategy!r}; "
            f"可选 {sorted(GENERATION_RETRIEVAL_STRATEGIES)}。"
            "换链路会让两轨不同源, 请显式用 --retrieval-strategy 声明并在台账里写明"
        )
    resolved_top_k = top_k if top_k is not None else retrieval.top_k
    resolved_theta = theta if theta is not None else retrieval.theta

    identity_gateway = build_model_gateway(settings)
    try:
        embedding_identity = {
            "model": identity_gateway.embedding_model,
            "provider": identity_gateway.embedding_provider,
            "revision": identity_gateway.embedding_revision,
        }
        chat_identity = {
            "model": identity_gateway.chat_model,
            "provider": identity_gateway.chat_provider,
        }
    finally:
        await identity_gateway.aclose()
    if embedding_identity != retrieval.embedding_identity:
        raise GenerationTrackNotReadyError(
            "当前 embedding 身份与检索轨 manifest 不一致, 两轨读的不是同一份向量: "
            f"manifest={retrieval.embedding_identity}, 当前={embedding_identity}"
        )

    # 语料必须与检索轨跑批时逐字节一致: 中间重建过 chunk, 端到端差异就不再只来自分块方式。
    async with session_factory() as session:
        try:
            preflight = await preflight_chunk_corpus(
                session,
                dataset_name=retrieval.dataset,
                origin=retrieval.origin,
                embedding_model=embedding_identity["model"],
                embedding_provider=embedding_identity["provider"],
                embedding_revision=embedding_identity["revision"],
            )
        except ChunkCorpusNotReadyError as error:
            raise GenerationTrackNotReadyError(f"四策略 corpus 前置检查失败: {error}") from error
    if preflight.corpus_fingerprint != retrieval.corpus_fingerprint:
        raise GenerationTrackNotReadyError(
            "chunk 语料在检索轨之后发生变化, 端到端结论无法与检索轨对齐: "
            f"manifest={retrieval.corpus_fingerprint}, 当前={preflight.corpus_fingerprint}"
        )
    if preflight.dataset_fingerprint != retrieval.dataset_fingerprint:
        raise GenerationTrackNotReadyError(
            "评测样本在检索轨之后发生变化: "
            f"manifest={retrieval.dataset_fingerprint}, 当前={preflight.dataset_fingerprint}"
        )

    run_ids: dict[ChunkStrategy, UUID] = {}
    reused: dict[ChunkStrategy, bool] = {}
    reports: dict[ChunkStrategy, str | None] = {}
    config_hashes: dict[ChunkStrategy, str] = {}
    # 第一个策略跑完才知道标注指纹, 之后三个都用它做前置校验:
    # 跑批中途改标注会立刻被挡下, 而不是产出四份不同源的报告。
    annotation_fingerprint: str | None = None
    for chunk_strategy in CHUNK_STRATEGIES:
        result = await run_generation_baseline(
            dataset_name=retrieval.dataset,
            label=f"{label}-{chunk_strategy}",
            origin=retrieval.origin,
            top_k=resolved_top_k,
            theta=resolved_theta,
            output_root=output_root / "runs",
            retrieval_strategy=strategy,
            chunk_strategy=chunk_strategy,
            rerank_candidate_text_mode=rerank_candidate_text_mode,
            lexical_mode=lexical_mode,
            expected_dataset_fingerprint=retrieval.dataset_fingerprint,
            expected_annotation_fingerprint=annotation_fingerprint,
            chunk_metadata={
                "corpus_fingerprint": preflight.corpus_fingerprint,
                "retrieval_manifest": str(manifest_path.resolve()),
                "retrieval_run_id": retrieval.run_ids.get(chunk_strategy),
                "summary": retrieval.chunk_summaries[chunk_strategy],
            },
            reuse_completed=reuse_completed,
            settings=settings,
        )
        run_ids[chunk_strategy] = result.run_id
        reused[chunk_strategy] = result.reused
        config_hashes[chunk_strategy] = result.config_hash
        reports[chunk_strategy] = (
            str(result.report_path.with_name("report.json").resolve())
            if result.report_path
            else None
        )
        if annotation_fingerprint is None:
            annotation_fingerprint = await _read_annotation_fingerprint(result.run_id)

    await _assert_single_variable(run_ids)

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    manifest_dir = output_root / f"{timestamp}-{_slug(label)}"
    manifest_dir.mkdir(parents=True, exist_ok=False)
    manifest_file = manifest_dir / "manifest.json"
    manifest = {
        "track": "generation",
        "dataset": retrieval.dataset,
        "label": label,
        "origin": retrieval.origin,
        "retrieval_strategy": strategy,
        "top_k": resolved_top_k,
        "theta": resolved_theta,
        "dataset_fingerprint": retrieval.dataset_fingerprint,
        "annotation_fingerprint": annotation_fingerprint,
        "corpus_fingerprint": preflight.corpus_fingerprint,
        "prompt_fingerprint": prompt_fingerprint(),
        "embedding_identity": embedding_identity,
        "chat_identity": chat_identity,
        "retrieval_manifest": {
            "path": str(manifest_path.resolve()),
            "label": retrieval.label,
            "run_ids": retrieval.run_ids,
        },
        "controlled_config_keys": list(CONTROLLED_CONFIG_KEYS),
        "runs": {
            chunk_strategy: {
                "run_id": str(run_ids[chunk_strategy]),
                "config_hash": config_hashes[chunk_strategy],
                "reused": reused[chunk_strategy],
                "report": reports[chunk_strategy],
            }
            for chunk_strategy in CHUNK_STRATEGIES
        },
    }
    manifest_file.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    await close_database()
    return GenerationBatchResult(
        manifest_path=manifest_file, run_ids=run_ids, reused=reused
    )


async def _read_annotation_fingerprint(run_id: UUID) -> str:
    async with session_factory() as session:
        value = (
            await session.execute(
                text("SELECT config->>'annotation_fingerprint' FROM eval_runs WHERE id=:id"),
                {"id": run_id},
            )
        ).scalar_one_or_none()
        await session.rollback()
    if not value:
        raise GenerationTrackNotReadyError(f"run {run_id} 缺少 annotation_fingerprint")
    return str(value)


async def _assert_single_variable(run_ids: dict[ChunkStrategy, UUID]) -> None:
    """回读四个 run 的 config, 逐键核对受控项。

    不信任内存里的变量而回读数据库: 复用的旧 run 是别的进程写的, 它到底跑在什么
    配置下, 只有落库的那份 config 说了算。
    """
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    text("SELECT id, config FROM eval_runs WHERE id=ANY(:ids)"),
                    {"ids": list(run_ids.values())},
                )
            )
            .mappings()
            .all()
        )
        await session.rollback()
    configs = {row["id"]: dict(row["config"] or {}) for row in rows}
    missing = [str(value) for value in run_ids.values() if value not in configs]
    if missing:
        raise GenerationTrackNotReadyError(f"run 未落库或已被删除: {missing}")

    baseline_strategy = CHUNK_STRATEGIES[0]
    baseline = configs[run_ids[baseline_strategy]]
    differences: list[str] = []
    for chunk_strategy in CHUNK_STRATEGIES:
        config = configs[run_ids[chunk_strategy]]
        if config.get("chunk_strategy") != chunk_strategy:
            differences.append(
                f"{chunk_strategy}: config.chunk_strategy={config.get('chunk_strategy')!r}"
            )
        if chunk_strategy == baseline_strategy:
            continue
        differences.extend(
            f"{chunk_strategy}.{key}: {baseline.get(key)!r} != {config.get(key)!r}"
            for key in CONTROLLED_CONFIG_KEYS
            if baseline.get(key) != config.get(key)
        )
    if differences:
        raise GenerationTrackNotReadyError(
            "四个生成 run 不是单变量对照, 受控配置出现差异: " + "; ".join(differences[:20])
        )


def _slug(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in value
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="以检索轨 manifest 为基准, 依次跑四个 E1 生成 run 并产出生成 manifest"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="chunk_strategy_runner 产出的检索轨 manifest.json",
    )
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--top-k", type=int, default=None, help="默认继承检索 manifest 的 top_k"
    )
    parser.add_argument("--theta", type=float, default=None)
    parser.add_argument(
        "--retrieval-strategy",
        choices=sorted(GENERATION_RETRIEVAL_STRATEGIES),
        default=None,
        help="默认继承检索 manifest 的链路",
    )
    parser.add_argument(
        "--rerank-candidate-text-mode",
        choices=["title_heading_content", "heading_content", "content"],
        default=None,
    )
    parser.add_argument("--lexical-mode", default=None)
    parser.add_argument("--no-reuse", action="store_true")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("eval/outputs/generation-strategies")
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = asyncio.run(
        run_generation_strategy_batch(
            manifest_path=args.manifest,
            label=args.label,
            output_root=args.output_dir,
            top_k=args.top_k,
            theta=args.theta,
            retrieval_strategy=args.retrieval_strategy,
            rerank_candidate_text_mode=args.rerank_candidate_text_mode,
            lexical_mode=args.lexical_mode,
            reuse_completed=not args.no_reuse,
        )
    )
    print(
        json.dumps(
            {
                "manifest": str(result.manifest_path),
                "run_ids": {key: str(value) for key, value in result.run_ids.items()},
                "reused": result.reused,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
