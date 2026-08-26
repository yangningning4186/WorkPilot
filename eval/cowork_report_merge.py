"""把 Cowork 完整 live 报告中的运行级异常替换为同配置重试 observation。

该入口只处理基础设施异常，不用于挑选更高分回答。原 observation 必须是
``failed``/``executing``，重试 observation 必须正常终止；suite、模型、预算、fixture
policy 与被测实现指纹必须一致。输出仍需交给 ``eval.cowork_runner --rescore-report``
按当前 scorer 重新计分后才能晋升 baseline。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


class CoworkRetryMergeError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CoworkRetryMergeError(f"无法读取报告 {path}: {error}") from error
    if not isinstance(payload, dict) or payload.get("kind") != "cowork":
        raise CoworkRetryMergeError(f"不是 Cowork 报告: {path}")
    if not isinstance(payload.get("items"), list):
        raise CoworkRetryMergeError(f"报告缺少 items: {path}")
    return payload


def _contract(report: dict[str, Any]) -> dict[str, Any]:
    manifest = report.get("manifest")
    manifest = manifest if isinstance(manifest, dict) else {}
    config = manifest.get("config")
    config = config if isinstance(config, dict) else {}
    reproducibility = manifest.get("reproducibility")
    reproducibility = reproducibility if isinstance(reproducibility, dict) else {}
    return {
        "suite": report.get("suite"),
        "suite_version": report.get("suite_version"),
        "suite_sha256": manifest.get("suite_sha256") or config.get("suite_sha256"),
        "model": manifest.get("model") or config.get("model"),
        "budgets": manifest.get("budgets") or config.get("budgets"),
        "runtime": config.get("runtime"),
        "fixture_policy": manifest.get("fixture_policy") or config.get("fixture_policy"),
        "implementation_fingerprint": reproducibility.get("implementation_fingerprint"),
    }


def _indexed(report: dict[str, Any], *, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in report["items"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("item_id"), str):
            raise CoworkRetryMergeError(f"{label} 含非法 item")
        item_id = raw["item_id"]
        if item_id in result:
            raise CoworkRetryMergeError(f"{label} 含重复 item_id: {item_id}")
        result[item_id] = raw
    return result


def merge_infrastructure_retries(
    source: dict[str, Any],
    retries: list[dict[str, Any]],
    *,
    source_path: Path,
    retry_paths: list[Path],
    label: str,
) -> dict[str, Any]:
    source_contract = _contract(source)
    source_items = _indexed(source, label="source")
    replacements: dict[str, dict[str, Any]] = {}
    audit: list[dict[str, Any]] = []

    for retry, retry_path in zip(retries, retry_paths, strict=True):
        if _contract(retry) != source_contract:
            raise CoworkRetryMergeError(f"重试报告合同漂移: {retry_path}")
        for item_id, replacement in _indexed(retry, label=str(retry_path)).items():
            if item_id not in source_items:
                raise CoworkRetryMergeError(f"重试含 source 不存在的 item: {item_id}")
            if item_id in replacements:
                raise CoworkRetryMergeError(f"同一 item 被多个重试报告替换: {item_id}")
            old_observation = source_items[item_id].get("observation")
            new_observation = replacement.get("observation")
            if not isinstance(old_observation, dict) or not isinstance(new_observation, dict):
                raise CoworkRetryMergeError(f"{item_id}: observation 非法")
            old_status = old_observation.get("status")
            new_status = new_observation.get("status")
            if old_status not in {"failed", "executing"}:
                raise CoworkRetryMergeError(
                    f"{item_id}: 原状态 {old_status!r} 不是运行级异常，禁止挑选替换"
                )
            if new_status not in {"done", "waiting_human"} or new_observation.get("error"):
                raise CoworkRetryMergeError(f"{item_id}: 重试仍未正常终止 status={new_status!r}")
            replacements[item_id] = replacement
            audit.append(
                {
                    "item_id": item_id,
                    "old_status": old_status,
                    "new_status": new_status,
                    "retry_report": str(retry_path.resolve()),
                    "retry_report_sha256": _sha256(retry_path),
                }
            )

    if not replacements:
        raise CoworkRetryMergeError("没有可合并的基础设施重试")
    merged = deepcopy(source)
    merged["run_id"] = str(uuid4())
    merged["label"] = label
    merged["items"] = [replacements.get(item["item_id"], item) for item in source["items"]]
    now = datetime.now(UTC).isoformat()
    merged["manifest"] = {
        "schema_version": "cowork-eval-infrastructure-retry-merge-manifest.v1",
        "run_id": merged["run_id"],
        "label": label,
        "started_at": now,
        "finished_at": now,
        "mode": "infrastructure_retry_merge_no_model_calls",
        "source_report": str(source_path.resolve()),
        "source_report_sha256": _sha256(source_path),
        "retry_replacements": sorted(audit, key=lambda value: value["item_id"]),
        "suite_sha256": source_contract["suite_sha256"],
        "model": source_contract["model"],
        "budgets": source_contract["budgets"],
        "fixture_policy": source_contract["fixture_policy"],
        "config": source.get("config"),
        "config_hash": source.get("config_hash"),
        "reproducibility": source.get("reproducibility"),
    }
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description="合并 Cowork 基础设施异常的同配置重试")
    parser.add_argument("source", type=Path)
    parser.add_argument("--retry", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"拒绝覆盖现有文件: {args.output}")
    source = _load(args.source)
    retries = [_load(path) for path in args.retry]
    merged = merge_infrastructure_retries(
        source,
        retries,
        source_path=args.source,
        retry_paths=args.retry,
        label=args.label,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(args.output)


if __name__ == "__main__":
    main()
