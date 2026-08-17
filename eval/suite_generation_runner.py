"""在已审计的 dev suite 上运行一套正式生成评测。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.retrieval.strategy import ChunkStrategy
from eval.generation_baseline import GenerationRunResult, run_generation_baseline
from eval.report_metrics import KIND_GENERATION, METRICS
from eval.suites import EvalSuite, load_suite, validate_suite


@dataclass(frozen=True)
class SuiteGenerationResult:
    manifest_path: Path
    reports: tuple[Path, ...]
    combined_report: Path


async def run_suite_generation(
    *,
    suite_path: Path,
    retrieval_manifest_path: Path,
    label: str,
    authorization_note: str,
    allow_model_send: bool,
    output_root: Path,
    chunk_strategy: ChunkStrategy = "heading",
    settings: Settings | None = None,
) -> SuiteGenerationResult:
    suite = load_suite(suite_path)
    _validate_dev_only_suite(suite)
    if not allow_model_send or not authorization_note.strip():
        raise ValueError("生成评测会发送问题与截断证据，必须显式授权并记录授权说明")
    settings = settings or Settings()
    retrieval = _load_retrieval_manifest(
        retrieval_manifest_path,
        suite=suite,
        suite_path=suite_path,
        chunk_strategy=chunk_strategy,
    )
    async with session_factory() as session:
        suite_audit = await validate_suite(session, suite)

    authorization_fingerprint = hashlib.sha256(
        authorization_note.strip().encode()
    ).hexdigest()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    batch_dir = output_root / f"{timestamp}-{_slug(label)}"
    batch_dir.mkdir(parents=True, exist_ok=False)
    reports: list[Path] = []
    runs: dict[str, dict[str, object]] = {}
    child_reports = retrieval["runs"][chunk_strategy]["child_reports"]
    for dataset, retrieval_report_path in zip(suite.datasets, child_reports, strict=True):
        retrieval_report = json.loads(Path(retrieval_report_path).read_text(encoding="utf-8"))
        result = await run_generation_baseline(
            dataset_name=dataset.name,
            label=f"{label}-{dataset.name}",
            origin=suite.origin,
            top_k=int(retrieval["config"]["top_k"]),
            theta=float(retrieval["config"]["theta"]),
            output_root=batch_dir / "runs",
            retrieval_strategy=str(retrieval["config"]["retrieval_strategy"]),
            chunk_strategy=chunk_strategy,
            rerank_candidate_text_mode=str(
                retrieval["config"]["rerank_candidate_text_mode"]
            ),
            lexical_mode=str(retrieval["config"]["lexical_mode"]),
            expected_dataset_fingerprint=str(
                retrieval_report["config"]["dataset_fingerprint"]
            ),
            chunk_metadata={
                "suite_definition_sha256": retrieval["suite_definition_sha256"],
                "retrieval_manifest": str(retrieval_manifest_path.resolve()),
                "retrieval_run_id": retrieval["runs"][chunk_strategy]["run_ids"][
                    dataset.name
                ],
                "authorization_note_fingerprint": authorization_fingerprint,
            },
            reuse_completed=False,
            settings=settings,
        )
        report_path = _required_report_path(result, dataset.name)
        reports.append(report_path)
        runs[dataset.name] = {
            "run_id": str(result.run_id),
            "config_hash": result.config_hash,
            "report": str(report_path.resolve()),
            "item_count": dataset.item_count,
        }

    combined_report = _combine_reports(
        suite=suite,
        suite_definition_sha256=str(retrieval["suite_definition_sha256"]),
        label=label,
        chunk_strategy=chunk_strategy,
        child_paths=reports,
        output_dir=batch_dir / chunk_strategy,
    )

    manifest = {
        "schema_version": 1,
        "track": "generation",
        "suite": suite.name,
        "suite_path": str(suite_path.resolve()),
        "suite_definition_sha256": retrieval["suite_definition_sha256"],
        "suite_audit": suite_audit,
        "test_access": {
            "included_datasets": [dataset.name for dataset in suite.datasets],
            "test_datasets": [],
            "passed": True,
        },
        "model_send_authorization": {
            "approved": True,
            "note": authorization_note.strip(),
            "note_fingerprint": authorization_fingerprint,
            "endpoint": settings.tier_main_base_url,
            "model": settings.tier_main_model,
            "data_scope": "70 dev questions and truncated retrieved evidence",
        },
        "config": {
            **retrieval["config"],
            "chunk_strategy": chunk_strategy,
            "retrieval_manifest": str(retrieval_manifest_path.resolve()),
        },
        "runs": runs,
        "combined_report": str(combined_report.resolve()),
    }
    manifest_path = batch_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    await close_database()
    return SuiteGenerationResult(
        manifest_path=manifest_path,
        reports=tuple(reports),
        combined_report=combined_report,
    )


def _combine_reports(
    *,
    suite: EvalSuite,
    suite_definition_sha256: str,
    label: str,
    chunk_strategy: ChunkStrategy,
    child_paths: list[Path],
    output_dir: Path,
) -> Path:
    """把四份分 dataset 的生成报告并成一份 70 条的 suite 报告。

    检索轨一直有这一步，生成轨此前没有——于是生成轨只有分 dataset 的报告，
    夜间门禁没有可以判的整套报告。按 dataset 分别过门禁不是等价替代：
    70 条会碎成四组，每组的配对样本更少，快照也要维护四份。
    """
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in child_paths]
    expected_datasets = [dataset.name for dataset in suite.datasets]
    actual_datasets = [str(payload["dataset"]) for payload in payloads]
    if actual_datasets != expected_datasets:
        raise ValueError(
            f"suite child report 顺序/集合漂移: expected={expected_datasets}, "
            f"actual={actual_datasets}"
        )
    configs = [payload["config"] for payload in payloads]
    _validate_child_configs(configs, chunk_strategy=chunk_strategy)
    items = [item for payload in payloads for item in payload["items"]]
    item_ids = [str(item["item_id"]) for item in items]
    if len(items) != suite.item_count or len(set(item_ids)) != suite.item_count:
        raise ValueError(
            f"suite item 数量或唯一性错误: rows={len(items)}, unique={len(set(item_ids))}"
        )
    dataset_fingerprints = _per_dataset(payloads, "dataset_fingerprint")
    # gold answer 与 constraints 的判据指纹逐 dataset 不同，但**不能就这么丢掉**：
    # 它是 constraint_pass 的判据身份，合成一个再存，标注改过就会显形
    annotation_fingerprints = _per_dataset(payloads, "annotation_fingerprint")
    config = dict(configs[0])
    config.update(
        {
            "dataset": suite.name,
            "dataset_fingerprint": _merge_fingerprints(dataset_fingerprints),
            "annotation_fingerprint": _merge_fingerprints(annotation_fingerprints),
            "chunk_strategy": chunk_strategy,
            "chunk_metadata": {
                "suite_definition_sha256": suite_definition_sha256,
                "dataset_fingerprints": dataset_fingerprints,
                "annotation_fingerprints": annotation_fingerprints,
                "retrieval_run_ids": {
                    str(payload["dataset"]): payload["config"]["chunk_metadata"][
                        "retrieval_run_id"
                    ]
                    for payload in payloads
                },
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
        "metrics": _aggregate_items(items, config),
        "items": items,
        "source_reports": [str(path.resolve()) for path in child_paths],
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    report_path = output_dir / "report.json"
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report_path


def _aggregate_items(
    items: list[dict[str, object]], config: dict[str, object]
) -> dict[str, object]:
    """聚合口径直接借 `report_metrics` 的 MetricSpec。

    不另写一份求和逻辑：门禁判的就是这些 spec，报告里的聚合值与门禁读到的
    必须是同一个定义，否则"报告上没退、门禁说退了"这种事迟早发生。
    micro 口径（分子分母分别累加）与 compare.py 的配对聚合一致。
    """
    completed = [item for item in items if item.get("error") is None]
    metrics: dict[str, object] = {
        "item_count": len(items),
        "completed_count": len(completed),
        "error_count": len(items) - len(completed),
    }
    for spec in METRICS[KIND_GENERATION]:
        numerator = 0.0
        denominator = 0.0
        eligible = 0
        for item in items:
            point = spec.extract(item, config)
            if not point.eligible:
                continue
            numerator += point.numerator
            denominator += point.denominator
            eligible += 1
        metrics[spec.name] = {
            "value": numerator / denominator if denominator else None,
            "eligible_items": eligible,
        }
    return metrics


def _per_dataset(payloads: list[dict[str, Any]], key: str) -> dict[str, str]:
    return {
        str(payload["dataset"]): str(payload["config"][key]) for payload in payloads
    }


def _merge_fingerprints(fingerprints: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(fingerprints, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# 这几项**天生**逐 dataset 不同（各自的语料与标注身份），不是实验变量，
# 所以不参与漂移比对；但它们全部被并进 combined config 的指纹与 chunk_metadata，
# 不是被丢掉。除此之外任何一项不同，都说明四次跑批不是同一套配置。
_PER_DATASET_CONFIG_KEYS = frozenset(
    {"dataset", "dataset_fingerprint", "annotation_fingerprint", "chunk_metadata"}
)


def _validate_child_configs(
    configs: list[dict[str, object]], *, chunk_strategy: ChunkStrategy
) -> None:
    def controlled(config: dict[str, object]) -> dict[str, object]:
        return {
            key: value
            for key, value in config.items()
            if key not in _PER_DATASET_CONFIG_KEYS
        }

    baseline = controlled(configs[0])
    suite_sha = {
        str(config["chunk_metadata"]["suite_definition_sha256"])  # type: ignore[index]
        for config in configs
    }
    if len(suite_sha) != 1:
        raise ValueError("suite child report 的 suite 定义指纹不一致")
    for config in configs:
        if config.get("chunk_strategy") != chunk_strategy:
            raise ValueError("child report 的 chunk_strategy 与目标不一致")
        if controlled(config) != baseline:
            raise ValueError("suite child report 除 dataset 外存在配置漂移")


def _validate_dev_only_suite(suite: EvalSuite) -> None:
    test_datasets = [dataset.name for dataset in suite.datasets if "test" in dataset.name.lower()]
    if test_datasets:
        raise ValueError(f"dev suite 禁止包含 test dataset: {test_datasets}")
    if suite.item_count != 70:
        raise ValueError(f"本轮扩展评测必须恰好 70 条 dev，实际 {suite.item_count}")
    if "agent_task" in suite.category_counts or len(suite.category_counts) != 6:
        raise ValueError("本轮必须是无 agent_task 的六类 dev suite")


def _load_retrieval_manifest(
    path: Path,
    *,
    suite: EvalSuite,
    suite_path: Path,
    chunk_strategy: ChunkStrategy,
) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected_sha = hashlib.sha256(suite_path.read_bytes()).hexdigest()
    if payload.get("suite") != suite.name or payload.get("suite_definition_sha256") != expected_sha:
        raise ValueError("检索 manifest 与当前 suite 名称或定义指纹不一致")
    if payload.get("test_access", {}).get("passed") is not True:
        raise ValueError("检索 manifest 未通过 test-access 审计")
    runs = payload.get("runs")
    if not isinstance(runs, dict) or chunk_strategy not in runs:
        raise ValueError(f"检索 manifest 缺少 {chunk_strategy} run")
    strategy_run = runs[chunk_strategy]
    if not isinstance(strategy_run, dict):
        raise TypeError(f"检索 manifest 的 {chunk_strategy} run 格式错误")
    expected_names = [dataset.name for dataset in suite.datasets]
    run_ids = strategy_run.get("run_ids")
    child_reports = strategy_run.get("child_reports")
    if not isinstance(run_ids, dict) or list(run_ids) != expected_names:
        raise ValueError("检索 run_ids 与 suite dataset 顺序/集合不一致")
    if not isinstance(child_reports, list) or len(child_reports) != len(expected_names):
        raise ValueError("检索 child_reports 未完整覆盖 suite")
    for name, report_path in zip(expected_names, child_reports, strict=True):
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        if report.get("dataset") != name or report.get("config", {}).get("chunk_strategy") != chunk_strategy:
            raise ValueError(f"检索 child report 与 suite 不一致: {name}")
    return payload


def _required_report_path(result: GenerationRunResult, dataset_name: str) -> Path:
    if result.report_path is None:
        raise RuntimeError(f"{dataset_name} 未导出生成 report")
    return result.report_path.with_name("report.json")


def _slug(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in value
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 70 条 dev suite 生成评测")
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--retrieval-manifest", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--allow-model-send", action="store_true")
    parser.add_argument("--authorization-note", default="")
    parser.add_argument(
        "--chunk-strategy",
        choices=["fixed", "heading", "recursive", "semantic"],
        default="heading",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("eval/outputs/dev-suite-generation")
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = asyncio.run(
        run_suite_generation(
            suite_path=args.suite,
            retrieval_manifest_path=args.retrieval_manifest,
            label=args.label,
            authorization_note=args.authorization_note,
            allow_model_send=args.allow_model_send,
            output_root=args.output_dir,
            chunk_strategy=args.chunk_strategy,
        )
    )
    print(
        json.dumps(
            {
                "manifest": str(result.manifest_path),
                "combined_report": str(result.combined_report),
                "reports": [str(path) for path in result.reports],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
