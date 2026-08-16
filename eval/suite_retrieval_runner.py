"""对一个不复制底层 gold 的多 dataset suite 运行并聚合检索评测。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.retrieval.strategy import CHUNK_STRATEGIES, ChunkStrategy
from eval.dense_baseline import (
    RETRIEVAL_STRATEGIES,
    BaselineRunResult,
    ItemResult,
    _aggregate,
    _markdown_report,
    run_dense_baseline,
)
from eval.metrics.refusal import analyze_refusal
from eval.suites import EvalSuite, load_suite, validate_suite


@dataclass(frozen=True)
class SuiteRetrievalResult:
    manifest_path: Path
    reports: dict[ChunkStrategy, Path]


async def run_suite_retrieval(
    *,
    suite_path: Path,
    label: str,
    chunk_strategies: tuple[ChunkStrategy, ...],
    retrieval_strategy: str,
    top_k: int,
    diagnostic_k: int,
    token_budget: int,
    theta: float,
    alpha: float,
    output_root: Path,
    rerank_candidate_text_mode: str | None = None,
    lexical_mode: str | None = None,
    settings: Settings | None = None,
) -> SuiteRetrievalResult:
    suite = load_suite(suite_path)
    _validate_dev_only_suite(suite)
    settings = settings or Settings()
    async with session_factory() as session:
        suite_audit = await validate_suite(session, suite)
    suite_definition_sha256 = hashlib.sha256(suite_path.read_bytes()).hexdigest()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    batch_dir = output_root / f"{timestamp}-{_slug(label)}"
    batch_dir.mkdir(parents=True, exist_ok=False)

    child_reports: dict[ChunkStrategy, list[Path]] = {}
    run_ids: dict[ChunkStrategy, dict[str, str]] = {}
    combined_reports: dict[ChunkStrategy, Path] = {}
    for chunk_strategy in chunk_strategies:
        child_reports[chunk_strategy] = []
        run_ids[chunk_strategy] = {}
        for dataset in suite.datasets:
            result = await run_dense_baseline(
                dataset_name=dataset.name,
                label=f"{label}-{chunk_strategy}-{dataset.name}",
                origin=suite.origin,
                top_k=top_k,
                diagnostic_k=diagnostic_k,
                token_budget=token_budget,
                theta=theta,
                alpha=alpha,
                output_root=batch_dir / "runs",
                strategy=retrieval_strategy,
                rerank_candidate_text_mode=rerank_candidate_text_mode,
                lexical_mode=lexical_mode,
                chunk_strategy=chunk_strategy,
                token_count_mode="unicode",
                reuse_completed=False,
                settings=settings,
            )
            report_path = _required_report_path(result, dataset.name)
            child_reports[chunk_strategy].append(report_path)
            run_ids[chunk_strategy][dataset.name] = str(result.run_id)
        combined_reports[chunk_strategy] = _combine_reports(
            suite=suite,
            suite_definition_sha256=suite_definition_sha256,
            label=label,
            chunk_strategy=chunk_strategy,
            child_paths=child_reports[chunk_strategy],
            output_dir=batch_dir / chunk_strategy,
        )

    manifest = {
        "schema_version": 1,
        "suite": suite.name,
        "suite_path": str(suite_path.resolve()),
        "suite_definition_sha256": suite_definition_sha256,
        "suite_audit": suite_audit,
        "test_access": {
            "included_datasets": [dataset.name for dataset in suite.datasets],
            "test_datasets": [],
            "passed": True,
        },
        "config": {
            "retrieval_strategy": retrieval_strategy,
            "chunk_strategies": list(chunk_strategies),
            "top_k": top_k,
            "diagnostic_k": diagnostic_k,
            "token_budget": token_budget,
            "theta": theta,
            "alpha": alpha,
            "origin": suite.origin,
            "rerank_candidate_text_mode": (
                rerank_candidate_text_mode or settings.rerank_candidate_text_mode
            ),
            "lexical_mode": lexical_mode or settings.lexical_mode,
        },
        "runs": {
            strategy: {
                "run_ids": run_ids[strategy],
                "child_reports": [str(path.resolve()) for path in child_reports[strategy]],
                "combined_report": str(combined_reports[strategy].resolve()),
            }
            for strategy in chunk_strategies
        },
    }
    manifest_path = batch_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    await close_database()
    return SuiteRetrievalResult(manifest_path=manifest_path, reports=combined_reports)


def _validate_dev_only_suite(suite: EvalSuite) -> None:
    test_datasets = [dataset.name for dataset in suite.datasets if "test" in dataset.name.lower()]
    if test_datasets:
        raise ValueError(f"dev suite 禁止包含 test dataset: {test_datasets}")
    if suite.item_count != 70:
        raise ValueError(f"本轮扩展评测必须恰好 70 条 dev，实际 {suite.item_count}")
    if "agent_task" in suite.category_counts or len(suite.category_counts) != 6:
        raise ValueError("本轮必须是无 agent_task 的六类 dev suite")


def _required_report_path(result: BaselineRunResult, dataset_name: str) -> Path:
    if result.report_path is None:
        raise RuntimeError(f"{dataset_name} 未导出 report，suite 跑批禁止复用无报告 run")
    return result.report_path.with_name("report.json")


def _combine_reports(
    *,
    suite: EvalSuite,
    suite_definition_sha256: str,
    label: str,
    chunk_strategy: ChunkStrategy,
    child_paths: list[Path],
    output_dir: Path,
) -> Path:
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in child_paths]
    expected_datasets = [dataset.name for dataset in suite.datasets]
    actual_datasets = [str(payload["dataset"]) for payload in payloads]
    if actual_datasets != expected_datasets:
        raise ValueError(
            f"suite child report 顺序/集合漂移: expected={expected_datasets}, actual={actual_datasets}"
        )
    configs = [payload["config"] for payload in payloads]
    _validate_child_configs(configs, chunk_strategy=chunk_strategy)
    items = [item for payload in payloads for item in payload["items"]]
    item_ids = [str(item["item_id"]) for item in items]
    if len(items) != suite.item_count or len(set(item_ids)) != suite.item_count:
        raise ValueError(
            f"suite item 数量或唯一性错误: rows={len(items)}, unique={len(set(item_ids))}"
        )
    results = [_item_result(item) for item in items]
    configured_threshold = float(configs[0]["refusal_threshold"])
    metrics = _aggregate(
        results,
        analyze_refusal(
            [(item.top_score, item.answerable) for item in results],
            configured_threshold=configured_threshold,
        ),
        configured_threshold=configured_threshold,
    )
    dataset_fingerprints = {
        str(payload["dataset"]): str(payload["config"]["dataset_fingerprint"])
        for payload in payloads
    }
    combined_fingerprint = hashlib.sha256(
        json.dumps(dataset_fingerprints, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    config = dict(configs[0])
    config.update(
        {
            "dataset": suite.name,
            "dataset_fingerprint": combined_fingerprint,
            "chunk_strategy": chunk_strategy,
            "chunk_metadata": {
                "suite_definition_sha256": suite_definition_sha256,
                "dataset_fingerprints": dataset_fingerprints,
            },
        }
    )
    config_hash = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload = {
        "run_id": f"suite-{config_hash[:24]}",
        "dataset": suite.name,
        "label": f"{label}-{chunk_strategy}",
        "git_sha": payloads[0]["git_sha"],
        "config": config,
        "config_hash": config_hash,
        "metrics": metrics,
        "items": items,
        "source_reports": [str(path.resolve()) for path in child_paths],
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    report_path = output_dir / "report.json"
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "report.md").write_text(_markdown_report(payload), encoding="utf-8")
    return report_path


def _validate_child_configs(
    configs: list[dict[str, object]], *, chunk_strategy: ChunkStrategy
) -> None:
    ignored = {"dataset", "dataset_fingerprint"}
    baseline = {key: value for key, value in configs[0].items() if key not in ignored}
    for config in configs:
        if config.get("chunk_strategy") != chunk_strategy:
            raise ValueError("child report 的 chunk_strategy 与目标不一致")
        current = {key: value for key, value in config.items() if key not in ignored}
        if current != baseline:
            raise ValueError("suite child report 除 dataset 外存在配置漂移")


def _item_result(payload: dict[str, object]) -> ItemResult:
    return ItemResult(
        item_id=UUID(str(payload["item_id"])),
        category=str(payload["category"]),
        question=str(payload["question"]),
        answerable=bool(payload["answerable"]),
        top_score=float(payload["top_score"]),
        latency_ms=int(payload["latency_ms"]),
        retrieval=payload.get("retrieval") if isinstance(payload.get("retrieval"), dict) else None,
        retrieved=list(payload.get("retrieved") or []),
        span_diagnostics=list(payload.get("span_diagnostics") or []),
    )


def _slug(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in value
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 70-dev 多数据集检索评测并聚合报告")
    parser.add_argument("--suite", type=Path, default=Path("eval/suites/m1-dev-70.json"))
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--chunk-strategy",
        action="append",
        choices=list(CHUNK_STRATEGIES),
        dest="chunk_strategies",
    )
    parser.add_argument(
        "--retrieval-strategy",
        choices=list(RETRIEVAL_STRATEGIES),
        default="dense-lexical-rrf-rerank",
    )
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--diagnostic-k", type=int, default=50)
    parser.add_argument("--token-budget", type=int, default=4000)
    parser.add_argument("--theta", type=float, default=0.5)
    parser.add_argument("--alpha", type=float, default=0.5)
    parser.add_argument("--rerank-candidate-text-mode", default=None)
    parser.add_argument("--lexical-mode", default=None)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("eval/outputs/dev-suite-retrieval")
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    strategies = tuple(args.chunk_strategies or ("heading", "semantic"))
    result = asyncio.run(
        run_suite_retrieval(
            suite_path=args.suite,
            label=args.label,
            chunk_strategies=strategies,
            retrieval_strategy=args.retrieval_strategy,
            top_k=args.top_k,
            diagnostic_k=args.diagnostic_k,
            token_budget=args.token_budget,
            theta=args.theta,
            alpha=args.alpha,
            output_root=args.output_dir,
            rerank_candidate_text_mode=args.rerank_candidate_text_mode,
            lexical_mode=args.lexical_mode,
        )
    )
    print(
        json.dumps(
            {
                "manifest": str(result.manifest_path),
                "reports": {key: str(value) for key, value in result.reports.items()},
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
