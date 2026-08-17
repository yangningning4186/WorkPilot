"""只重试 P1-J generation 报告中的传输错误项。"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm.gateway import build_model_gateway
from app.llm.providers.openai_compatible import ProviderResponseError
from app.retrieval.citations import CitationValidationError
from app.services.grounded_answer import answer_with_settings


async def retry_errors(
    *,
    reports: list[Path],
    output_dir: Path,
    reranker_base_url: str,
    authorization_note: str,
    max_attempts: int = 3,
) -> Path:
    if not authorization_note.strip():
        raise ValueError("必须记录开发数据发送授权")
    failed: list[dict[str, Any]] = []
    for path in reports:
        payload = json.loads(path.read_text(encoding="utf-8"))
        failed.extend(
            {**item, "dataset": payload["dataset"]}
            for item in payload["items"]
            if item.get("error") is not None
        )
    if not failed:
        raise ValueError("报告中没有失败项")

    settings = Settings().model_copy(
        update={
            "llm_cache_enabled": False,
            "rerank_enabled": True,
            "rerank_candidate_k": 50,
            "reranker_base_url": reranker_base_url,
            "rerank_evidence_gate_max_chars": 3000,
        }
    )
    rows: list[dict[str, Any]] = []
    async with session_factory() as session:
        gateway = build_model_gateway(settings)
        try:
            for item in failed:
                last_error: str | None = None
                started = time.monotonic()
                for attempt in range(1, max_attempts + 1):
                    try:
                        result = await answer_with_settings(
                            session,
                            gateway,
                            query=str(item["question"]),
                            top_k=5,
                            settings=settings,
                        )
                    except (httpx.HTTPError, ProviderResponseError) as error:
                        last_error = f"{type(error).__name__}: {error}"
                        if attempt < max_attempts:
                            await asyncio.sleep(0.5)
                        continue
                    except CitationValidationError as error:
                        last_error = f"CitationValidationError: {error}"
                        break
                    rows.append(
                        {
                            "dataset": item["dataset"],
                            "item_id": item["item_id"],
                            "category": item["category"],
                            "question": item["question"],
                            "answerable": item["answerable"],
                            "attempts": attempt,
                            "refused": result.refused,
                            "refusal_reason": result.refusal_reason,
                            "evidence_sufficient": result.evidence_sufficient,
                            "evidence_reason": result.evidence_reason,
                            "rerank_applied": result.rerank_applied,
                            "latency_ms": round((time.monotonic() - started) * 1000),
                            "error": None,
                        }
                    )
                    last_error = None
                    break
                if last_error is not None:
                    rows.append(
                        {
                            "dataset": item["dataset"],
                            "item_id": item["item_id"],
                            "category": item["category"],
                            "question": item["question"],
                            "answerable": item["answerable"],
                            "attempts": max_attempts,
                            "refused": None,
                            "error": last_error,
                            "latency_ms": round((time.monotonic() - started) * 1000),
                        }
                    )
            await session.commit()
        finally:
            await gateway.aclose()
    await close_database()

    payload = {
        "schema_version": 1,
        "source_reports": [str(path.resolve()) for path in reports],
        "authorization_note": authorization_note.strip(),
        "config": {
            "rerank_enabled": True,
            "rerank_candidate_k": 50,
            "final_top_k": 5,
            "rerank_evidence_gate_max_chars": 3000,
            "cache_enabled": False,
            "max_attempts": max_attempts,
        },
        "item_count": len(rows),
        "success_count": sum(row["error"] is None for row in rows),
        "items": rows,
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    report = output_dir / "report.json"
    report.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="重试 P1-J generation 传输错误")
    parser.add_argument("--report", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--reranker-base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--authorization-note", required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = asyncio.run(
        retry_errors(
            reports=args.report,
            output_dir=args.output_dir,
            reranker_base_url=args.reranker_base_url,
            authorization_note=args.authorization_note,
        )
    )
    print(json.dumps({"report": str(report)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
