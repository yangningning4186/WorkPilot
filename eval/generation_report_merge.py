"""把 generation 完整报告中的基础设施错误替换为同合同定点重试。

只允许替换原本 ``error != null`` 的 observation；正常完成但质量低的题永远不能被挑选
替换。suite、git SHA、模型、KB/index、route、被测实现指纹与所有实验配置（除定点
``selection``）必须一致，合并后用正式 ``MetricSpec`` 从 70 条 observations 重新聚合。
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

from eval.generation_runner import _aggregate


class GenerationRetryMergeError(RuntimeError):
    pass


_INFRASTRUCTURE_ERRORS = (
    "ProviderRouteTimeoutError:",
    "ProviderTimeoutError:",
    "TimeoutError:",
    "ReadTimeout:",
    "APIConnectionError:",
    "ConnectError:",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GenerationRetryMergeError(f"无法读取报告 {path}: {error}") from error
    if not isinstance(payload, dict) or payload.get("kind") != "generation":
        raise GenerationRetryMergeError(f"不是 generation 报告: {path}")
    if not isinstance(payload.get("items"), list):
        raise GenerationRetryMergeError(f"报告缺少 items: {path}")
    return payload


def _experiment_config(report: dict[str, Any]) -> dict[str, Any]:
    raw = report.get("config")
    if not isinstance(raw, dict):
        raise GenerationRetryMergeError("报告缺少 config")
    config = deepcopy(raw)
    config.pop("selection", None)
    return config


def _contract(report: dict[str, Any]) -> dict[str, Any]:
    suite = report.get("suite") if isinstance(report.get("suite"), dict) else {}
    reproducibility = (
        report.get("reproducibility")
        if isinstance(report.get("reproducibility"), dict)
        else {}
    )
    kb = report.get("kb") if isinstance(report.get("kb"), dict) else {}
    return {
        "kind": report.get("kind"),
        "dataset": report.get("dataset"),
        "suite_sha256": suite.get("sha256"),
        "git_sha": report.get("git_sha"),
        "config": _experiment_config(report),
        "kb": kb,
        "git_dirty": reproducibility.get("git_dirty"),
        "implementation_fingerprint": reproducibility.get("implementation_fingerprint"),
    }


def _indexed(report: dict[str, Any], *, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in report["items"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("item_id"), str):
            raise GenerationRetryMergeError(f"{label} 含非法 item")
        item_id = raw["item_id"]
        if item_id in result:
            raise GenerationRetryMergeError(f"{label} 含重复 item_id: {item_id}")
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
    if source_contract["git_dirty"] is not False:
        raise GenerationRetryMergeError("source 不是 clean Git 正式报告")
    source_items = _indexed(source, label="source")
    replacements: dict[str, dict[str, Any]] = {}
    audit: list[dict[str, Any]] = []

    for retry, retry_path in zip(retries, retry_paths, strict=True):
        if _contract(retry) != source_contract:
            raise GenerationRetryMergeError(f"重试报告合同漂移: {retry_path}")
        retry_selection = retry.get("config", {}).get("selection")
        for item_id, replacement in _indexed(retry, label=str(retry_path)).items():
            if not isinstance(retry_selection, list) or item_id not in retry_selection:
                raise GenerationRetryMergeError(f"{item_id}: 重试 selection 未显式包含该题")
            if item_id not in source_items:
                raise GenerationRetryMergeError(f"重试含 source 不存在的 item: {item_id}")
            if item_id in replacements:
                raise GenerationRetryMergeError(f"同一 item 被多个重试报告替换: {item_id}")
            old_error = source_items[item_id].get("error")
            if not isinstance(old_error, str) or not old_error.startswith(_INFRASTRUCTURE_ERRORS):
                raise GenerationRetryMergeError(
                    f"{item_id}: 原结果不是允许的基础设施错误，禁止挑选替换"
                )
            if replacement.get("error") is not None:
                raise GenerationRetryMergeError(f"{item_id}: 重试仍有错误")
            expected_model = source["config"].get("chat_model")
            expected_provider = source["config"].get("chat_provider")
            if (
                replacement.get("model") != expected_model
                or replacement.get("provider") != expected_provider
            ):
                raise GenerationRetryMergeError(f"{item_id}: 重试实际模型身份漂移")
            replacements[item_id] = replacement
            audit.append(
                {
                    "item_id": item_id,
                    "old_error": old_error,
                    "retry_report": str(retry_path),
                    "retry_report_sha256": _sha256(retry_path),
                    "replacement_attempts": replacement.get("attempts"),
                }
            )

    if not replacements:
        raise GenerationRetryMergeError("没有可合并的基础设施重试")
    merged = deepcopy(source)
    merged["run_id"] = str(uuid4())
    merged["label"] = label
    merged["generated_at"] = datetime.now(UTC).isoformat()
    merged["items"] = [replacements.get(item["item_id"], item) for item in source["items"]]
    merged["metrics"] = _aggregate(merged["items"], merged["config"])
    merged["infrastructure_retry_merge"] = {
        "schema_version": "workpilot-generation-infrastructure-retry-merge.v1",
        "mode": "observation_merge_no_model_calls",
        "source_report": str(source_path),
        "source_report_sha256": _sha256(source_path),
        "replacements": sorted(audit, key=lambda value: value["item_id"]),
    }
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description="合并 generation 基础设施异常的同合同重试")
    parser.add_argument("source", type=Path)
    parser.add_argument("--retry", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"拒绝覆盖现有文件: {args.output}")
    merged = merge_infrastructure_retries(
        _load(args.source),
        [_load(path) for path in args.retry],
        source_path=args.source,
        retry_paths=args.retry,
        label=args.label,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
