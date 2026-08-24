"""汇总 A5 盲评：校验 owner 评分完整性后解盲，不接受脚本代填。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from eval.stats import MetricSamples, RatioPoint, paired_bootstrap


class MemoryReviewError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewScore:
    item_id: str
    memory_off: int
    memory_on: int
    preferred: Literal["memory_off", "memory_on", "tie"]
    reviewer: str
    reviewed_at: str


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise MemoryReviewError(f"{path}:{line_no} 不是 JSON 对象")
        rows.append(value)
    return rows


def load_review_scores(package: Path) -> list[ReviewScore]:
    records = _read_jsonl(package / "records.jsonl")
    reviews = _read_jsonl(package / "blind-review.jsonl")
    if len(records) < 5 or len(records) != len(reviews):
        raise MemoryReviewError("records 与 blind review 必须逐条配对且至少 5 条")
    scores: list[ReviewScore] = []
    seen: set[str] = set()
    for index, (record, review) in enumerate(zip(records, reviews, strict=True)):
        item_id = str(record.get("item_id", ""))
        if item_id != review.get("item_id") or not item_id or item_id in seen:
            raise MemoryReviewError("盲评 item_id 顺序漂移或重复")
        seen.add(item_id)
        rating_a = review.get("rating_a_1_to_5")
        rating_b = review.get("rating_b_1_to_5")
        preferred = review.get("preferred")
        reviewer = str(review.get("reviewer") or "").strip()
        reviewed_at = str(review.get("reviewed_at") or "").strip()
        if (
            isinstance(rating_a, bool)
            or not isinstance(rating_a, int)
            or not 1 <= rating_a <= 5
            or isinstance(rating_b, bool)
            or not isinstance(rating_b, int)
            or not 1 <= rating_b <= 5
        ):
            raise MemoryReviewError(f"{item_id} 的 A/B 评分必须是 1 到 5 的整数")
        if preferred not in {"A", "B", "tie"}:
            raise MemoryReviewError(f"{item_id} 的 preferred 必须是 A/B/tie")
        if not reviewer or not reviewed_at:
            raise MemoryReviewError(f"{item_id} 缺 reviewer 或 reviewed_at")

        # runner 按 item 序号交替交换 A/B；review 文件里不落臂名，保持评分时盲态。
        swapped = index % 2 == 1
        off_rating, on_rating = (rating_b, rating_a) if swapped else (rating_a, rating_b)
        if preferred == "tie":
            unblinded_preference: Literal["memory_off", "memory_on", "tie"] = "tie"
        elif (preferred == "A") ^ swapped:
            unblinded_preference = "memory_off"
        else:
            unblinded_preference = "memory_on"
        scores.append(
            ReviewScore(
                item_id=item_id,
                memory_off=off_rating,
                memory_on=on_rating,
                preferred=unblinded_preference,
                reviewer=reviewer,
                reviewed_at=reviewed_at,
            )
        )
    return scores


def summarize_review(scores: list[ReviewScore]) -> dict[str, Any]:
    if len(scores) < 5:
        raise MemoryReviewError("满意度汇总至少需要 5 条配对评分")
    result = paired_bootstrap(
        {
            "owner_satisfaction": MetricSamples(
                baseline=tuple(RatioPoint(float(score.memory_off), 1.0) for score in scores),
                candidate=tuple(RatioPoint(float(score.memory_on), 1.0) for score in scores),
            )
        },
        seed=20260818,
        resamples=5000,
    )["owner_satisfaction"]
    preferences = {"memory_off": 0, "memory_on": 0, "tie": 0}
    for score in scores:
        preferences[score.preferred] += 1
    return {
        "items": len(scores),
        "owner_satisfaction": {
            "memory_off": result.baseline,
            "memory_on": result.candidate,
            "delta": result.delta,
            "ci_low": result.ci_low,
            "ci_high": result.ci_high,
            "verdict": result.verdict,
        },
        "preference_counts": preferences,
        "reviewers": sorted({score.reviewer for score in scores}),
        "reviewed_at": {
            "first": min(score.reviewed_at for score in scores),
            "last": max(score.reviewed_at for score in scores),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="A5 owner 盲评汇总")
    parser.add_argument("--package", type=Path, required=True)
    args = parser.parse_args()
    summary = summarize_review(load_review_scores(args.package))
    target = args.package / "review-report.json"
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
