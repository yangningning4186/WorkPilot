"""A3 · 用证据编号替代模型逐字抄写的首轮可靠性实验。

同一轮输出依次经过严格 schema、编号存在性和原文切片校验。模型只选择正文中给出的
E 编号，服务端按字符区间回填 evidence_quotes；不使用模糊匹配或语义相似度。

运行：

    PYTHONPATH=backend backend/.venv/bin/python -m eval.agent_evidence_anchor_experiment \
      --limit 20 --label a3-20260816
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.agent.review_tools import (
    CARD_SYSTEM_PROMPT,
    ReviewToolResponseError,
    _bounded_document,
    build_evidence_catalog,
    parse_card_payload,
    repair_instruction,
    resolve_card_evidence,
)
from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm.gateway import build_model_gateway
from app.llm.types import Message


class ExperimentError(RuntimeError):
    pass


@dataclass
class LoadedDocument:
    document_id: str
    title: str
    full_text: str


@dataclass
class AnchorRecord:
    document_id: str
    title: str
    success: bool
    rounds: int
    first_round_ok: bool
    protocol: str | None
    evidence_count: int
    evidence_refs: list[str]
    evidence_quotes: list[str]
    resolved_quotes: list[str]
    failure_kinds: list[str]
    error: str | None


def _classify(error: ReviewToolResponseError) -> str:
    message = str(error)
    if "schema 非法" in message:
        return "schema_invalid"
    if "不是 JSON 对象" in message:
        return "not_json"
    if "evidence_refs" in message:
        return "invalid_ref"
    if "逐字摘录" in message:
        return "quote_not_verbatim"
    return "other"


async def _load_documents(limit: int) -> list[LoadedDocument]:
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        """
                        SELECT d.id AS document_id, d.title, v.full_text
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
        LoadedDocument(
            document_id=str(row["document_id"]),
            title=str(row["title"]),
            full_text=str(row["full_text"]),
        )
        for row in rows
    ]


async def main() -> int:
    parser = argparse.ArgumentParser(description="A3 evidence_refs 原文锚定实验")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--label", required=True)
    parser.add_argument("--max-tokens", type=int, default=2400)
    parser.add_argument("--repair-attempts", type=int, default=2)
    parser.add_argument(
        "--output-root", type=Path, default=Path("eval/outputs/agent-evidence-anchor")
    )
    args = parser.parse_args()

    documents = await _load_documents(args.limit)
    if len(documents) < 5:
        raise ExperimentError(f"可用文档只有 {len(documents)} 篇，样本太少不出结论")

    gateway = build_model_gateway(Settings())
    records: list[AnchorRecord] = []
    try:
        for document in documents:
            excerpt = _bounded_document(document.full_text, 30_000)
            catalog = build_evidence_catalog(excerpt)
            catalog_by_ref = {item.ref: item for item in catalog}
            conversation = [
                Message(role="system", content=CARD_SYSTEM_PROMPT),
                Message(
                    role="user",
                    content=json.dumps(
                        {
                            "title": document.title,
                            "document": [
                                {"ref": item.ref, "text": item.text}
                                for item in catalog
                            ],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            ]
            errors: list[ReviewToolResponseError] = []
            payload = None
            resolved: list[str] = []
            for attempt in range(args.repair_attempts + 1):
                result = await gateway.complete(
                    conversation,
                    task_type="eval_agent_evidence_anchor",
                    max_tokens=args.max_tokens,
                    temperature=0.0,
                )
                try:
                    payload = parse_card_payload(result.text)
                    resolved = resolve_card_evidence(
                        payload, excerpt, evidence_catalog=catalog_by_ref
                    )
                except ReviewToolResponseError as error:
                    errors.append(error)
                    if attempt < args.repair_attempts:
                        conversation.extend(
                            [
                                Message(role="assistant", content=result.text),
                                Message(
                                    role="user",
                                    content=repair_instruction(error, "model_facing"),
                                ),
                            ]
                        )
                    continue
                break
            if payload is None or not resolved:
                last_error = errors[-1]
                records.append(
                    AnchorRecord(
                        document_id=document.document_id,
                        title=document.title,
                        success=False,
                        rounds=len(errors),
                        first_round_ok=False,
                        protocol=None,
                        evidence_count=0,
                        evidence_refs=[],
                        evidence_quotes=[],
                        resolved_quotes=[],
                        failure_kinds=[_classify(error) for error in errors],
                        error=str(last_error),
                    )
                )
                continue
            protocol = "refs" if payload.evidence_refs else "legacy_quotes"
            records.append(
                AnchorRecord(
                    document_id=document.document_id,
                    title=document.title,
                    success=True,
                    rounds=len(errors) + 1,
                    first_round_ok=not errors,
                    protocol=protocol,
                    evidence_count=len(resolved),
                    evidence_refs=payload.evidence_refs,
                    evidence_quotes=payload.evidence_quotes,
                    resolved_quotes=resolved,
                    failure_kinds=[_classify(error) for error in errors],
                    error=None,
                )
            )
    finally:
        await gateway.aclose()
        await close_database()

    package = args.output_root / args.label
    package.mkdir(parents=True, exist_ok=True)
    with (package / "records.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    failures: dict[str, int] = {}
    for record in records:
        for kind in record.failure_kinds:
            failures[kind] = failures.get(kind, 0) + 1
    successful = [record for record in records if record.success]
    first_round_successful = [record for record in records if record.first_round_ok]
    recovered = [
        record for record in records if not record.first_round_ok and record.success
    ]
    report: dict[str, Any] = {
        "label": args.label,
        "generated_at": datetime.now(UTC).isoformat(),
        "model_identity": {
            "chat_model": gateway.chat_model,
            "chat_provider": gateway.chat_provider,
        },
        "temperature": 0.0,
        "max_tokens": args.max_tokens,
        "repair_attempts": args.repair_attempts,
        "documents": len(records),
        "prompt_sha": hashlib.sha256(CARD_SYSTEM_PROMPT.encode()).hexdigest()[:16],
        "first_round_success": len(first_round_successful),
        "first_round_success_rate": round(
            len(first_round_successful) / len(records), 4
        ),
        "recovered": len(recovered),
        "recovery_rate": round(
            len(recovered) / (len(records) - len(first_round_successful)), 4
        ),
        "final_success": len(successful),
        "final_success_rate": round(len(successful) / len(records), 4),
        "refs_protocol_success": sum(
            record.protocol == "refs" for record in successful
        ),
        "legacy_quote_success": sum(
            record.protocol == "legacy_quotes" for record in successful
        ),
        "resolved_quotes_are_source_slices": all(
            quote in _bounded_document(document.full_text, 30_000)
            for record, document in zip(records, documents, strict=True)
            for quote in record.resolved_quotes
        ),
        "failure_kind_counts": failures,
    }
    (package / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
