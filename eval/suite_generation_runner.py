"""在已审计的 dev suite 上运行一套正式生成评测。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.retrieval.strategy import ChunkStrategy
from eval.generation_baseline import GenerationRunResult, run_generation_baseline
from eval.suites import EvalSuite, load_suite, validate_suite


@dataclass(frozen=True)
class SuiteGenerationResult:
    manifest_path: Path
    reports: tuple[Path, ...]


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
    }
    manifest_path = batch_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    await close_database()
    return SuiteGenerationResult(manifest_path=manifest_path, reports=tuple(reports))


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
                "reports": [str(path) for path in result.reports],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
