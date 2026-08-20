"""A1 · 面向模型的错误信息对工具失败恢复的影响。

**假设**：工具校验失败后，把"哪一条不合法、违反哪条约束、下一步怎么做"回传给模型，
比只回一句"输出不合法"更能让它自己改对。

**被测单元**是 `extract_card` 的一次调用：模型读一篇文档，输出结构化卡片，
工具做 schema 校验与 `evidence_quotes` 逐字校验。三档只在**校验失败之后**分叉：

| 档 | 失败后 |
|---|---|
| `none` | 不补救，首轮失败即工具失败（引入补救机制之前的行为） |
| `generic` | 回一句"上一次输出不合法，请重新输出" |
| `model_facing` | 回具体到条目的可执行指令（约束 4 的写法） |

首轮 prompt 三档完全相同，所以 `tool_error_rate` 是对照的**完整性检查**而不是结论：
三档若差得多，说明温度 0 下仍有明显采样噪声，此时 `recovery_rate` 的差也不能当结论。

跑法：

    PYTHONPATH=backend backend/.venv/bin/python -m eval.agent_error_message_experiment \
      --limit 20 --label a1-20260816

产出 `eval/outputs/agent-error-messages/<label>/report.json` 与逐样本 `records.jsonl`。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm_bootstrap import build_model_gateway
from app.rag.review.state import ReviewDocument
from app.rag.review.tools import (
    CARD_SYSTEM_PROMPT,
    CardErrorStyle,
    DatabaseModelReviewTools,
    ReviewToolResponseError,
)

CONDITIONS: tuple[CardErrorStyle, ...] = get_args(CardErrorStyle)


class ExperimentError(RuntimeError):
    """实验前置条件不满足；fail-closed，不出半份数据。"""


@dataclass
class CardRecord:
    condition: str
    document_id: str
    title: str
    rounds: int
    first_round_ok: bool
    final_ok: bool
    failure_kinds: list[str]
    error: str | None


@dataclass
class ConditionSummary:
    condition: str
    items: int
    first_round_failures: int
    tool_error_rate: float
    recovered: int
    recovery_rate: float | None
    mean_rounds_on_failure: float | None
    final_success_rate: float
    failure_kind_counts: dict[str, int] = field(default_factory=dict)


def _classify(message: str) -> str:
    if "逐字摘录" in message:
        return "quote_not_verbatim"
    if "schema 非法" in message:
        return "schema_invalid"
    if "不是 JSON 对象" in message:
        return "not_json"
    return "other"


async def _load_documents(session: AsyncSession, limit: int) -> list[ReviewDocument]:
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT d.id AS document_id, v.id AS version_id, d.title, d.source_uri
                    FROM documents d
                    JOIN document_versions v ON v.document_id = d.id
                    WHERE d.deleted_at IS NULL
                      AND v.activated_at IS NOT NULL
                      AND v.invalid_at IS NULL
                      AND v.parse_status = 'done'
                      AND length(v.full_text) > 500
                    ORDER BY d.title
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "document_id": str(row["document_id"]),
            "version_id": str(row["version_id"]),
            "title": str(row["title"]),
            "source_uri": str(row["source_uri"]),
        }
        for row in rows
    ]


def _summarize(condition: str, records: list[CardRecord]) -> ConditionSummary:
    items = len(records)
    failures = [record for record in records if not record.first_round_ok]
    recovered = [record for record in failures if record.final_ok]
    kinds: dict[str, int] = {}
    for record in records:
        for kind in record.failure_kinds:
            kinds[kind] = kinds.get(kind, 0) + 1
    return ConditionSummary(
        condition=condition,
        items=items,
        first_round_failures=len(failures),
        tool_error_rate=round(len(failures) / items, 4) if items else 0.0,
        recovered=len(recovered),
        recovery_rate=(
            round(len(recovered) / len(failures), 4) if failures else None
        ),
        mean_rounds_on_failure=(
            round(sum(record.rounds for record in failures) / len(failures), 2)
            if failures
            else None
        ),
        final_success_rate=(
            round(sum(record.final_ok for record in records) / items, 4) if items else 0.0
        ),
        failure_kind_counts=kinds,
    )


async def run_condition(
    condition: CardErrorStyle,
    documents: list[ReviewDocument],
    *,
    settings: Settings,
    repair_attempts: int,
    system_prompt: str | None = None,
    max_tokens: int = 1200,
) -> list[CardRecord]:
    records: list[CardRecord] = []
    async with session_factory() as session:
        gateway = build_model_gateway(settings)
        try:
            tools = DatabaseModelReviewTools(
                session,
                gateway,
                card_repair_attempts=repair_attempts,
                card_error_style=condition,
                card_system_prompt=system_prompt or CARD_SYSTEM_PROMPT,
                card_max_tokens=max_tokens,
            )
            for document in documents:
                before = len(tools.card_trace)
                error_text: str | None = None
                try:
                    await tools.extract_card(document)
                    final_ok = True
                except ReviewToolResponseError as error:
                    final_ok = False
                    error_text = str(error)
                trace = tools.card_trace[before] if len(tools.card_trace) > before else []
                failure_kinds = [_classify(item) for item in trace if item != "ok"]
                records.append(
                    CardRecord(
                        condition=condition,
                        document_id=document["document_id"],
                        title=document["title"],
                        rounds=len(trace),
                        first_round_ok=bool(trace) and trace[0] == "ok",
                        final_ok=final_ok,
                        failure_kinds=failure_kinds,
                        error=error_text,
                    )
                )
        finally:
            await gateway.aclose()
    return records


async def main() -> int:
    parser = argparse.ArgumentParser(description="A1 面向模型的错误信息实验")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--repair-attempts", type=int, default=2)
    parser.add_argument("--label", required=True)
    parser.add_argument(
        "--output-root", type=Path, default=Path("eval/outputs/agent-error-messages")
    )
    args = parser.parse_args()

    settings = Settings()

    async with session_factory() as session:
        documents = await _load_documents(session, args.limit)
    if len(documents) < 5:
        raise ExperimentError(f"可用文档只有 {len(documents)} 篇，样本太少不出结论")

    identity_gateway = build_model_gateway(settings)
    identity = {
        "chat_model": identity_gateway.chat_model,
        "chat_provider": identity_gateway.chat_provider,
    }
    await identity_gateway.aclose()

    all_records: list[CardRecord] = []
    summaries: list[ConditionSummary] = []
    for condition in CONDITIONS:
        # `none` 不补救，补救轮数对它无意义，显式置 0 免得报告里出现误导性的参数。
        attempts = 0 if condition == "none" else args.repair_attempts
        records = await run_condition(
            condition, documents, settings=settings, repair_attempts=attempts
        )
        all_records.extend(records)
        summaries.append(_summarize(condition, records))

    package = args.output_root / args.label
    package.mkdir(parents=True, exist_ok=True)
    with (package / "records.jsonl").open("w", encoding="utf-8") as handle:
        for record in all_records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    # 首轮 prompt 三档相同，首轮失败集合应当高度一致；不一致就是采样噪声的量。
    first_round_failed = {
        summary.condition: {
            record.document_id
            for record in all_records
            if record.condition == summary.condition and not record.first_round_ok
        }
        for summary in summaries
    }
    baseline = first_round_failed["none"]
    noise = {
        condition: sorted(failed.symmetric_difference(baseline))
        for condition, failed in first_round_failed.items()
        if condition != "none"
    }

    report: dict[str, Any] = {
        "label": args.label,
        "generated_at": datetime.now(UTC).isoformat(),
        "model_identity": identity,
        # 三档路由要到 W5 才做，当前网关只有一个 provider，不存在 fallback 链；
        # 记成 "not_configured" 而不是 false，免得日后有了 fallback 还以为这批数据关过。
        "fallback": "not_configured_single_provider",
        "temperature": 0.0,
        "repair_attempts": args.repair_attempts,
        "documents": len(documents),
        "system_prompt_sha": hashlib.sha256(CARD_SYSTEM_PROMPT.encode()).hexdigest()[:16],
        "conditions": [asdict(summary) for summary in summaries],
        "first_round_disagreement_vs_none": noise,
    }
    (package / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    await close_database()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
