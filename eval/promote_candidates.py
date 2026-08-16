"""把复核通过的候选题从 `synthetic/pending_human` 升为 `human`。

这是**评测资产里最不可逆的一步**：升 human 之后，这批题就成了后续所有指标与结论的
基准（CLAUDE.md：标注是这个项目最贵的资产）。因此这里只做一件事，但做严：

- 升级前逐条比对落库内容与 manifest 指纹，任何漂移直接中止；
- gold span 必须仍然锚得住原文（`validate_eval_spans`），否则标注已经废了；
- 复核人身份与时间必须显式给出并写进 `candidate_review`，
  只翻 `origin` 而不留痕等于事后无法分辨这批到底复核过没有；
- 幂等：已升过的再跑一次不报错也不重复改写。

    PYTHONPATH=backend backend/.venv/bin/python -m eval.promote_candidates \\
      --items eval/outputs/dataset-handwritten-40/<fp>/items.json \\
      --reviewer "姓名 <邮箱>" --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import close_database, session_factory
from eval.build_m1_candidate_suite import NAMESPACE, CandidateSuiteError
from eval.import_handwritten_suite import SUITE_NAME, TARGET_DATASETS, load_items


async def _load_stored(session: AsyncSession, item_ids: list[Any]) -> dict[Any, dict[str, Any]]:
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT i.id, i.origin, i.category, i.question, i.gold_answer,
                           i.gold_spans, i.constraints, i.difficulty,
                           d.name AS dataset,
                           validate_eval_spans(i.gold_spans) AS spans_valid
                    FROM eval_items i JOIN eval_datasets d ON d.id=i.dataset_id
                    WHERE i.id = ANY(:ids)
                    """
                ),
                {"ids": item_ids},
            )
        )
        .mappings()
        .all()
    )
    return {row["id"]: dict(row) for row in rows}


async def promote(
    items_path: Path, *, reviewer: str, note: str, apply: bool
) -> dict[str, Any]:
    if not reviewer.strip():
        raise CandidateSuiteError("必须显式给出复核人；升 human 不留痕等于无法追溯")
    items = load_items(items_path)
    expected = {uuid5(NAMESPACE, f"item:{item.item_key}"): item for item in items}
    reviewed_at = datetime.now(UTC).isoformat()

    async with session_factory() as session:
        stored = await _load_stored(session, list(expected))
        missing = sorted(str(i) for i in set(expected) - set(stored))
        if missing:
            raise CandidateSuiteError(f"这些候选不在库中，先导入再升级: {missing[:5]}")

        drifted: list[str] = []
        stale: list[str] = []
        for item_id, item in expected.items():
            row = stored[item_id]
            if not row["spans_valid"]:
                stale.append(item.item_key)
            same = (
                row["category"] == item.category
                and row["question"] == item.question
                and row["gold_answer"] == item.gold_answer
                and row["gold_spans"] == list(item.gold_spans)
                and int(row["difficulty"]) == item.difficulty
            )
            if not same:
                drifted.append(item.item_key)
        if stale:
            raise CandidateSuiteError(
                f"gold span 已经锚不住原文（解析版本变了？），拒绝升级: {stale[:5]}"
            )
        if drifted:
            raise CandidateSuiteError(
                f"落库内容与 manifest 不一致，复核对象与升级对象不是同一批: {drifted[:5]}"
            )

        already = [
            expected[i].item_key for i, row in stored.items() if row["origin"] == "human"
        ]
        pending = [i for i, row in stored.items() if row["origin"] != "human"]
        summary: dict[str, Any] = {
            "suite": SUITE_NAME,
            "items": len(expected),
            "already_human": len(already),
            "to_promote": len(pending),
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "datasets": sorted(TARGET_DATASETS.values()),
        }
        if not apply:
            await session.rollback()
            summary["applied"] = False
            return summary

        # 前面的校验读操作隐式开了事务，先收掉再开写事务
        await session.rollback()
        async with session.begin():
            for item_id in pending:
                item = expected[item_id]
                review = dict(item.constraints.get("candidate_review") or {})
                review.update(
                    {
                        "status": "approved",
                        "reviewer": reviewer,
                        "reviewed_at": reviewed_at,
                        "review_note": note,
                        "promoted_from": "synthetic",
                    }
                )
                constraints = {**item.constraints, "candidate_review": review}
                await session.execute(
                    text(
                        """
                        UPDATE eval_items
                        SET origin='human', constraints=CAST(:constraints AS jsonb)
                        WHERE id=:id AND origin<>'human'
                        """
                    ),
                    {"id": item_id, "constraints": json.dumps(constraints, ensure_ascii=False)},
                )

        after = await _load_stored(session, list(expected))
        bad = [
            expected[i].item_key
            for i, row in after.items()
            if row["origin"] != "human"
            or (row["constraints"].get("candidate_review") or {}).get("status") != "approved"
            or not row["spans_valid"]
        ]
        if bad:
            raise CandidateSuiteError(f"升级后校验失败: {bad[:5]}")
    await close_database()
    summary["applied"] = True
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="把复核通过的候选题升为 human")
    parser.add_argument("--items", type=Path, required=True)
    parser.add_argument("--reviewer", required=True, help="复核人，例如 \"姓名 <邮箱>\"")
    parser.add_argument("--note", default="逐条复核通过", help="复核备注")
    parser.add_argument("--apply", action="store_true", help="不加则只做 dry-run 校验")
    args = parser.parse_args()
    summary = asyncio.run(
        promote(args.items, reviewer=args.reviewer, note=args.note, apply=args.apply)
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
