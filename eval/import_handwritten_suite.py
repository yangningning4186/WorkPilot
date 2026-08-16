"""把人工撰写的候选题导入隔离 staging dataset。

与 `build_m1_candidate_suite` 的自动草稿走**同一套导入纪律**（确定性 uuid5、
幂等 upsert、内容漂移即失败、`validate_eval_spans` 校验），只是数据来源不同：
这里的题目由人阅读原文后撰写，`items.json` 由 authoring 脚本解析出精确字符区间。

导入的一律是 `origin=synthetic` / `review_status=pending_human`。**升 human 必须
由作者逐条确认**——助手读原文选证据、写综述答案，不等于 CLAUDE.md 说的"亲手标注"。

    PYTHONPATH=backend backend/.venv/bin/python -m eval.import_handwritten_suite \\
      --items eval/outputs/dataset-handwritten-40/<fp>/items.json --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

from app.core.db import close_database, session_factory

from eval.build_m1_candidate_suite import (
    CandidateItem,
    CandidateSuiteError,
    Language,
    Split,
    audit_content_quality,
    import_candidates,
)

SUITE_NAME = "m1-handwritten-40-v1"
VERSION = "handwritten-1"
TARGET_DATASETS: dict[tuple[Split, Language], str] = {
    ("dev", "zh"): "core-dev-handwritten-zh-v1",
    ("dev", "en"): "core-dev-handwritten-en-v1",
    ("test", "zh"): "core-test-handwritten-zh-v1",
    ("test", "en"): "core-test-handwritten-en-v1",
}


def load_items(path: Path) -> list[CandidateItem]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise CandidateSuiteError(f"items 必须是非空数组: {path}")
    items: list[CandidateItem] = []
    for raw in payload:
        split, language = raw["split"], raw["language"]
        key = (split, language)
        if key not in TARGET_DATASETS:
            raise CandidateSuiteError(f"未知的 split/language 组合: {key}")
        spans = tuple(raw["gold_spans"])
        items.append(
            CandidateItem(
                # item_key 加套件前缀：uuid5 由它派生，避免与自动草稿套件撞 ID
                item_key=f"{SUITE_NAME}:{raw['item_key']}",
                group_key=",".join(raw.get("source_docs") or ["none"]),
                language=language,
                split=split,
                dataset=TARGET_DATASETS[key],
                category=raw["category"],
                question=raw["question"],
                gold_answer=raw.get("gold_answer"),
                gold_spans=spans,
                gold_tools=tuple(raw.get("gold_tools") or ()),
                constraints=raw["constraints"],
                difficulty=int(raw.get("difficulty", 2)),
                origin="synthetic",
                temporal_ctx=raw.get("temporal_ctx"),
                partition_version_id=spans[0]["version_id"] if spans else "",
            )
        )
    return items


def preflight(items: list[CandidateItem]) -> dict[str, Any]:
    """写库前的 fail-closed 校验。任一不满足直接中止，不做部分导入。"""
    keys = [item.item_key for item in items]
    if len(set(keys)) != len(keys):
        raise CandidateSuiteError("item_key 重复")
    questions = [item.question.strip() for item in items]
    if len(set(questions)) != len(questions):
        raise CandidateSuiteError("问题重复")

    quality = audit_content_quality(items)
    if quality["status"] != "passed":
        raise CandidateSuiteError(f"内容质量门禁未通过: {quality['finding_counts']}")

    # dev / test 不得共用任何 document version，否则 test 不再是留出集
    by_split: dict[str, set[str]] = {"dev": set(), "test": set()}
    for item in items:
        by_split[item.split].update(str(s["version_id"]) for s in item.gold_spans)
    leaked = by_split["dev"] & by_split["test"]
    if leaked:
        raise CandidateSuiteError(f"dev/test 共用 document version: {sorted(leaked)}")

    for item in items:
        if item.origin != "synthetic":
            raise CandidateSuiteError(f"{item.item_key}: origin 必须是 synthetic")
        status = (item.constraints.get("candidate_review") or {}).get("status")
        if status != "pending_human":
            raise CandidateSuiteError(f"{item.item_key}: review_status 必须是 pending_human")

    return {
        "item_count": len(items),
        "content_quality": quality["status"],
        "category_counts": dict(sorted(Counter(i.category for i in items).items())),
        "split_counts": dict(sorted(Counter(i.split for i in items).items())),
        "language_counts": dict(sorted(Counter(i.language for i in items).items())),
        "dev_test_version_overlap": 0,
        "gold_span_count": sum(len(i.gold_spans) for i in items),
    }


async def run(items_path: Path, *, apply: bool) -> dict[str, Any]:
    items = load_items(items_path)
    summary = preflight(items)
    summary["suite"] = SUITE_NAME
    summary["datasets"] = sorted(TARGET_DATASETS.values())
    if not apply:
        summary["import"] = {"applied": False, "mode": "dry-run"}
        return summary
    async with session_factory() as session:
        imported = await import_candidates(
            session,
            items,
            datasets=TARGET_DATASETS,
            suite_name=SUITE_NAME,
            version=VERSION,
        )
    await close_database()
    summary["import"] = {"applied": True, "datasets": imported}
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="导入人工撰写候选题到隔离 staging dataset")
    parser.add_argument("--items", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="不加则只做 dry-run 校验")
    args = parser.parse_args()
    summary = asyncio.run(run(args.items, apply=args.apply))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
