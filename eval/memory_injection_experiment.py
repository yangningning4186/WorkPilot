"""A5 · 长期记忆注入开/关的严格配对实验。

每个样本用同一个模型、温度和 token 上限分别跑 memory_off / memory_on；执行顺序按
样本 ID 交替，避免永远让某一臂先跑。规则轨只判样本里冻结的 must_include / must_not_include，
用户满意度输出成盲评模板，绝不由脚本伪造。

合成 suite 只能做工程 smoke。正式产品结论必须换成 owner 审核的 human suite，并完成盲评。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from app.core.config import Settings
from app.llm_bootstrap import build_model_gateway
from app.rag.memory.prompt import (
    MEMORY_CONTEXT_PREFIX,
    MEMORY_CONTEXT_SUFFIX,
    MEMORY_USAGE_POLICY,
    escape_memory_fact,
)
from eval.stats import MetricSamples, RatioPoint, paired_bootstrap
from workpilot_ai.types import Message

SYSTEM_PROMPT = f"""你是个人工作助手。准确完成用户请求；不知道用户信息时直接说明不知道。
{MEMORY_USAGE_POLICY}
用中文回答，保持简洁。"""

FORBIDDEN_MEMORY_DISCLOSURES = (
    "[m",
    "(m",
    "（m",
    "<user_context",
    "</user_context",
    "user_context",
    "<personal_memory",
    "</personal_memory",
    "根据记忆",
    "根据您提供的个人记忆",
    "根据你提供的个人记忆",
    "根据您提供的背景信息",
    "根据你提供的背景信息",
    "根据背景信息",
    "根据您提供的信息",
    "根据你提供的信息",
    "个人记忆中",
    "《个人记忆》",
)
INTERNAL_MEMORY_LABELS = {
    "[m": re.compile(r"\[m\d+\]", re.IGNORECASE),
    "(m": re.compile(r"\(m\d+\)", re.IGNORECASE),
    "（m": re.compile(r"（m\d+）", re.IGNORECASE),
}


class MemoryExperimentError(RuntimeError):
    pass


@dataclass(frozen=True)
class MemoryCase:
    id: str
    query: str
    memories: list[str]
    must_include: list[str]
    must_not_include: list[str]


@dataclass(frozen=True)
class ArmRecord:
    condition: Literal["memory_off", "memory_on"]
    answer: str
    success: bool
    missing: list[str]
    forbidden_hits: list[str]
    latency_ms: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class PairedRecord:
    item_id: str
    query: str
    order: list[str]
    memory_off: ArmRecord
    memory_on: ArmRecord


def load_suite(path: Path) -> tuple[dict[str, Any], list[MemoryCase]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise MemoryExperimentError("A5 suite schema_version 必须为 1")
    items = raw.get("items")
    if not isinstance(items, list) or len(items) < 5:
        raise MemoryExperimentError("A5 paired 实验至少需要 5 条样本")
    cases: list[MemoryCase] = []
    seen: set[str] = set()
    for raw_item in items:
        if not isinstance(raw_item, dict):
            raise MemoryExperimentError("A5 item 必须是对象")
        case = MemoryCase(
            id=str(raw_item.get("id", "")).strip(),
            query=str(raw_item.get("query", "")).strip(),
            memories=_string_list(raw_item.get("memories"), "memories"),
            must_include=_string_list(raw_item.get("must_include"), "must_include"),
            must_not_include=_string_list(
                raw_item.get("must_not_include", []), "must_not_include", allow_empty=True
            ),
        )
        if not case.id or case.id in seen or not case.query or not case.memories:
            raise MemoryExperimentError("A5 item 的 id/query/memories 非法或 id 重复")
        seen.add(case.id)
        cases.append(case)
    return raw, cases


def _string_list(value: object, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise MemoryExperimentError(f"{field} 必须是非空字符串数组")
    normalized = [str(item).strip() for item in value]
    if any(not item for item in normalized):
        raise MemoryExperimentError(f"{field} 不能包含空字符串")
    return normalized


def render_memory_context(memories: list[str]) -> str:
    lines = "".join(f"- {escape_memory_fact(fact)}\n" for fact in memories)
    return MEMORY_CONTEXT_PREFIX + lines + MEMORY_CONTEXT_SUFFIX


def evaluate_answer(answer: str, case: MemoryCase) -> tuple[bool, list[str], list[str]]:
    folded = answer.casefold()
    missing = [term for term in case.must_include if term.casefold() not in folded]
    forbidden = find_disclosure_hits(answer, extra_terms=case.must_not_include)
    return not missing and not forbidden, missing, forbidden


def find_disclosure_hits(answer: str, *, extra_terms: list[str] | None = None) -> list[str]:
    """返回确定性来源泄漏命中；语义 Judge 也必须服从这条硬失败轨。"""

    folded = answer.casefold()
    forbidden_terms = tuple(
        dict.fromkeys((*(extra_terms or []), *FORBIDDEN_MEMORY_DISCLOSURES))
    )
    hits: list[str] = []
    for term in forbidden_terms:
        label_pattern = INTERNAL_MEMORY_LABELS.get(term.casefold())
        if label_pattern is not None:
            if label_pattern.search(answer):
                hits.append(term)
        elif term.casefold() in folded:
            hits.append(term)
    return hits


async def run_experiment(
    cases: list[MemoryCase], *, settings: Settings, max_tokens: int
) -> tuple[list[PairedRecord], dict[str, str]]:
    gateway = build_model_gateway(settings, mode="evaluation")
    identity = {
        "chat_model": gateway.chat_model,
        "chat_provider": gateway.chat_provider,
    }
    records: list[PairedRecord] = []
    try:
        for index, case in enumerate(cases):
            order: list[Literal["memory_off", "memory_on"]] = (
                ["memory_off", "memory_on"] if index % 2 == 0 else ["memory_on", "memory_off"]
            )
            arms: dict[str, ArmRecord] = {}
            for condition in order:
                context = render_memory_context(case.memories) if condition == "memory_on" else ""
                user_content = (
                    case.query if not context else f"{context}\n\n当前请求：\n{case.query}"
                )
                started = monotonic()
                result = await gateway.complete(
                    [
                        Message(role="system", content=SYSTEM_PROMPT),
                        Message(role="user", content=user_content),
                    ],
                    task_type="eval_memory_injection",
                    max_tokens=max_tokens,
                    temperature=0.0,
                )
                success, missing, forbidden = evaluate_answer(result.text, case)
                arms[condition] = ArmRecord(
                    condition=condition,
                    answer=result.text.strip(),
                    success=success,
                    missing=missing,
                    forbidden_hits=forbidden,
                    latency_ms=max(0, round((monotonic() - started) * 1000)),
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                )
            records.append(
                PairedRecord(
                    item_id=case.id,
                    query=case.query,
                    order=list(order),
                    memory_off=arms["memory_off"],
                    memory_on=arms["memory_on"],
                )
            )
    finally:
        await gateway.aclose()
    return records, identity


def summarize(records: list[PairedRecord]) -> dict[str, Any]:
    off = tuple(RatioPoint(float(record.memory_off.success), 1.0) for record in records)
    on = tuple(RatioPoint(float(record.memory_on.success), 1.0) for record in records)
    bootstrap = paired_bootstrap(
        {"task_success_rate": MetricSamples(baseline=off, candidate=on)},
        seed=20260818,
        resamples=5000,
    )["task_success_rate"]
    return {
        "items": len(records),
        "task_success_rate": {
            "memory_off": bootstrap.baseline,
            "memory_on": bootstrap.candidate,
            "delta": bootstrap.delta,
            "ci_low": bootstrap.ci_low,
            "ci_high": bootstrap.ci_high,
            "verdict": bootstrap.verdict,
        },
        "mean_latency_ms": {
            "memory_off": round(sum(item.memory_off.latency_ms for item in records) / len(records)),
            "memory_on": round(sum(item.memory_on.latency_ms for item in records) / len(records)),
        },
        "mean_input_tokens": {
            "memory_off": round(
                sum(item.memory_off.input_tokens for item in records) / len(records), 2
            ),
            "memory_on": round(
                sum(item.memory_on.input_tokens for item in records) / len(records), 2
            ),
        },
        "user_satisfaction": "pending_owner_blind_review",
    }


def _blind_rows(records: list[PairedRecord]) -> list[dict[str, Any]]:
    rows = []
    for index, record in enumerate(records):
        swap = index % 2 == 1
        first = record.memory_on if swap else record.memory_off
        second = record.memory_off if swap else record.memory_on
        rows.append(
            {
                "item_id": record.item_id,
                "query": record.query,
                "answer_a": first.answer,
                "answer_b": second.answer,
                "preferred": None,
                "rating_a_1_to_5": None,
                "rating_b_1_to_5": None,
                "reason": None,
                "reviewer": None,
                "reviewed_at": None,
            }
        )
    return rows


async def main() -> int:
    parser = argparse.ArgumentParser(description="A5 长期记忆注入 paired 实验")
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--max-tokens", type=int, default=500)
    parser.add_argument("--allow-model-send", action="store_true")
    parser.add_argument("--authorization-note", default="")
    parser.add_argument("--allow-synthetic", action="store_true")
    parser.add_argument("--output-root", type=Path, default=Path("eval/outputs/memory-injection"))
    args = parser.parse_args()
    suite_raw, cases = load_suite(args.suite)
    origin = suite_raw.get("origin")
    if not args.allow_model_send or not args.authorization_note.strip():
        raise MemoryExperimentError("发送 A5 问题与记忆前必须显式授权并记录 authorization-note")
    if origin != "human" and not args.allow_synthetic:
        raise MemoryExperimentError("合成 A5 suite 只能加 --allow-synthetic 做工程 smoke")
    if not 1 <= args.max_tokens <= 2000:
        raise MemoryExperimentError("max_tokens 必须位于 1 到 2000")

    records, identity = await run_experiment(cases, settings=Settings(), max_tokens=args.max_tokens)
    package = args.output_root / args.label
    package.mkdir(parents=True, exist_ok=False)
    with (package / "records.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    with (package / "blind-review.jsonl").open("w", encoding="utf-8") as handle:
        for row in _blind_rows(records):
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    suite_sha = hashlib.sha256(args.suite.read_bytes()).hexdigest()
    report = {
        "label": args.label,
        "generated_at": datetime.now(UTC).isoformat(),
        "experiment": "A5_memory_injection_paired",
        "suite": str(args.suite),
        "suite_sha256": suite_sha,
        "origin": origin,
        "claim_scope": (
            "product_quality" if origin == "human" else "engineering_only_no_product_claim"
        ),
        "authorization_note": args.authorization_note,
        "model_identity": identity,
        "routing_mode": "evaluation_no_fallback",
        "temperature": 0.0,
        "max_tokens": args.max_tokens,
        "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        "metrics": summarize(records),
    }
    (package / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
