"""把独立 KB 校准跑批晋升为可审计的拒答阈值文件。

校准报告和正式 evaluation 报告必须来自不同 suite。这个模块只负责验证校准报告并冻结
其中的 macro-F1 最优阈值；两份 suite SHA 是否独立，由 KB runner 在加载阈值时再次校验。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.kb_retrieval_runner import (
    REFUSAL_CALIBRATION_SCHEMA,
    resolve_actual_score_source,
)

METHOD = "macro_f1_grid_v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reviewed_at(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("reviewed_at 必须是 ISO-8601 时间") from error
    if parsed.tzinfo is None:
        raise ValueError("reviewed_at 必须包含时区")
    return value


def build_calibration(
    report_path: Path,
    *,
    reviewer: str,
    reviewed_at: str,
) -> dict[str, Any]:
    raw = report_path.read_bytes()
    report = json.loads(raw)
    if not isinstance(report, Mapping) or report.get("kind") != "retrieval":
        raise ValueError("校准输入必须是 retrieval report")
    if not reviewer.strip():
        raise ValueError("校准晋升必须提供 reviewer")
    reviewed_at = _reviewed_at(reviewed_at.strip())

    suite = report.get("suite")
    suite = suite if isinstance(suite, Mapping) else {}
    if (
        suite.get("review_status") != "approved"
        or not isinstance(suite.get("reviewer"), str)
        or not isinstance(suite.get("reviewed_at"), str)
    ):
        raise ValueError("校准 suite 必须先完成人工 approved")
    _reviewed_at(str(suite["reviewed_at"]))
    dataset_sha256 = suite.get("sha256")
    if not isinstance(dataset_sha256, str) or _SHA256.fullmatch(dataset_sha256) is None:
        raise ValueError("校准报告缺少合法 suite.sha256")

    reproducibility = report.get("reproducibility")
    reproducibility = reproducibility if isinstance(reproducibility, Mapping) else {}
    if reproducibility.get("git_dirty") is not False:
        raise ValueError("Git dirty 或状态缺失的校准报告不能晋升")
    git_sha = report.get("git_sha")
    if not isinstance(git_sha, str) or _GIT_SHA.fullmatch(git_sha) is None:
        raise ValueError("校准报告缺少合法 Git SHA")

    items = report.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("校准报告缺少 items")
    if any(not isinstance(item, Mapping) or item.get("error") is not None for item in items):
        raise ValueError("含失败样本的校准报告不能晋升")
    actual_score_source = resolve_actual_score_source(
        [dict(item) for item in items if isinstance(item, Mapping)],
        rerank_required=False,
    )
    config = report.get("config")
    config = config if isinstance(config, Mapping) else {}
    if config.get("retrieval_score_source") != actual_score_source:
        raise ValueError("校准报告声明的 score source 与逐题真实来源不一致")

    metrics = report.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    refusal = metrics.get("refusal")
    refusal = refusal if isinstance(refusal, Mapping) else {}
    best = refusal.get("best")
    best = best if isinstance(best, Mapping) else {}
    threshold = best.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, int | float):
        raise TypeError("校准报告缺少拒答最优阈值")
    threshold = float(threshold)
    if not math.isfinite(threshold):
        raise ValueError("校准阈值必须是有限数字")
    answerable_count = refusal.get("answerable_count")
    unanswerable_count = refusal.get("unanswerable_count")
    if not isinstance(answerable_count, int) or answerable_count < 1:
        raise ValueError("校准集至少需要一条可答题")
    if not isinstance(unanswerable_count, int) or unanswerable_count < 1:
        raise ValueError("校准集至少需要一条不可答题")

    dataset = report.get("dataset")
    if not isinstance(dataset, str) or not dataset.strip():
        raise ValueError("校准报告缺少 dataset")
    config_hash = report.get("config_hash")
    if not isinstance(config_hash, str) or _SHA256.fullmatch(config_hash) is None:
        raise ValueError("校准报告缺少合法 config_hash")

    payload: dict[str, Any] = {
        "schema_version": REFUSAL_CALIBRATION_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": dataset,
        "dataset_sha256": dataset_sha256,
        "source_report_sha256": hashlib.sha256(raw).hexdigest(),
        "source_config_sha256": config_hash,
        "git_sha": git_sha,
        "score_source": actual_score_source,
        "threshold": threshold,
        "method": METHOD,
        "answerable_count": answerable_count,
        "unanswerable_count": unanswerable_count,
        "suite_review": {
            "origin": suite.get("origin"),
            "status": suite.get("review_status"),
            "reviewer": suite.get("reviewer"),
            "reviewed_at": suite.get("reviewed_at"),
        },
        "reviewer": reviewer.strip(),
        "reviewed_at": reviewed_at,
    }
    payload["integrity"] = {"algorithm": "sha256", "value": _canonical_hash(payload)}
    return payload


def write_calibration(
    report_path: Path,
    *,
    reviewer: str,
    reviewed_at: str,
    output: Path,
) -> Path:
    if output.exists():
        raise ValueError(f"输出已存在，拒绝覆盖: {output}")
    payload = build_calibration(report_path, reviewer=reviewer, reviewed_at=reviewed_at)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--reviewed-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(
            write_calibration(
                args.report,
                reviewer=args.reviewer,
                reviewed_at=args.reviewed_at,
                output=args.output,
            )
        )
        return 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"校准晋升被拒绝：{error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
