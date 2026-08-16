"""Fail-closed audit for the M1 120-item candidate review files."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.core.db import close_database, session_factory
from sqlalchemy import bindparam, text

EXPECTED_SPLIT_CATEGORY_COUNTS = {
    "dev": Counter(
        {
            "single_hop": 11,
            "multi_hop": 12,
            "table": 0,
            "temporal": 10,
            "unanswerable": 7,
            "global": 10,
            "agent_task": 10,
        }
    ),
    "test": Counter(
        {
            "single_hop": 5,
            "multi_hop": 4,
            "table": 2,
            "temporal": 2,
            "unanswerable": 3,
            "global": 2,
            "agent_task": 2,
        }
    ),
}
EXPECTED_LANGUAGE_COUNTS = {
    ("dev", "zh"): 30,
    ("dev", "en"): 30,
    ("test", "zh"): 10,
    ("test", "en"): 10,
}
ANSWERABLE_WITH_SPANS = {"single_hop", "multi_hop", "table", "temporal"}
REQUIRED_REVIEW_FIELDS = {
    "item_key",
    "dataset",
    "split",
    "language",
    "category",
    "question",
    "gold_answer",
    "gold_spans",
    "gold_tools",
    "constraints",
    "difficulty",
    "temporal_ctx",
    "partition_version_id",
    "origin",
    "review_status",
    "reviewer",
    "reviewed_at",
    "review_note",
}
GENERIC_PATTERNS = (
    re.compile(r"核心结论是什么"),
    re.compile(r"可以得到什么结论"),
    re.compile(r"文档强调了哪两个要点"),
    re.compile(r"给出了商业产品的精确年度订阅价格"),
    re.compile(r"core claim does .* make", re.IGNORECASE),
    re.compile(r"conclusion follows by combining", re.IGNORECASE),
    re.compile(r"which two points does .* emphasize", re.IGNORECASE),
    re.compile(r"exact annual subscription price", re.IGNORECASE),
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {error}") from error
    return records


def _normalize_question(question: str) -> str:
    return " ".join(re.findall(r"\w+", question.casefold(), flags=re.UNICODE))


def _question_skeleton(question: str) -> str:
    value = re.sub(r'《[^》]+》|"[^"]+"', "<slot>", question.casefold())
    value = re.sub(r"\([^)]*evidence block[^)]*\)", "", value)
    return " ".join(value.split())


async def audit(output_dir: Path) -> dict[str, Any]:
    items = _load_jsonl(output_dir / "review-dev.jsonl") + _load_jsonl(
        output_dir / "review-test.jsonl"
    )
    failures: list[str] = []
    warnings: list[str] = []

    if len(items) != 80:
        failures.append(f"candidate count is {len(items)}, expected 80")
    split_counts = Counter(str(item.get("split")) for item in items)
    if split_counts != {"dev": 60, "test": 20}:
        failures.append(f"split counts: {dict(split_counts)}")
    language_counts = Counter(
        (str(item.get("split")), str(item.get("language"))) for item in items
    )
    if language_counts != EXPECTED_LANGUAGE_COUNTS:
        failures.append(f"language counts: {dict(language_counts)}")
    split_category_counts = {
        split: Counter(
            str(item.get("category")) for item in items if item.get("split") == split
        )
        for split in ("dev", "test")
    }
    for split, expected in EXPECTED_SPLIT_CATEGORY_COUNTS.items():
        if split_category_counts[split] != expected:
            failures.append(
                f"{split} category counts: actual={dict(split_category_counts[split])}, "
                f"expected={dict(expected)}"
            )

    item_keys = [str(item.get("item_key")) for item in items]
    if len(set(item_keys)) != len(item_keys):
        failures.append("duplicate item_key")
    normalized_questions: dict[str, list[str]] = defaultdict(list)
    skeletons: Counter[str] = Counter()
    raw_block_answers: list[str] = []
    generic_questions: list[str] = []
    placeholder_questions: list[str] = []
    missing_partition_versions: list[str] = []
    incomplete_schema: list[str] = []
    empty_tool_arguments: list[str] = []
    partition_version_ids: set[str] = set()
    version_splits: dict[str, set[str]] = defaultdict(set)
    span_keys: dict[tuple[str, int, int], list[tuple[str, str]]] = defaultdict(list)
    span_records: list[tuple[str, int, int, str, str, str]] = []

    for item in items:
        key = str(item.get("item_key"))
        split = str(item.get("split"))
        category = str(item.get("category"))
        question = str(item.get("question") or "")
        answer = item.get("gold_answer")
        spans = item.get("gold_spans") or []
        tools = item.get("gold_tools") or []
        partition_version = str(item.get("partition_version_id") or "")
        missing_fields = REQUIRED_REVIEW_FIELDS - set(item)
        if missing_fields:
            incomplete_schema.append(f"{key}:{','.join(sorted(missing_fields))}")
        normalized_questions[_normalize_question(question)].append(key)
        skeletons[_question_skeleton(question)] += 1
        if any(pattern.search(question) for pattern in GENERIC_PATTERNS):
            generic_questions.append(key)
        if re.search(r"\bblock\s+\d+\b", question, re.IGNORECASE):
            placeholder_questions.append(key)
        if item.get("language") == "en" and re.search(r"[\u3400-\u9fff]", question):
            failures.append(f"{key}: English question contains CJK")
        if item.get("language") == "zh" and not re.search(r"[\u3400-\u9fff]", question):
            failures.append(f"{key}: Chinese question lacks CJK")
        if (
            item.get("origin") != "synthetic"
            or item.get("review_status") != "pending_human"
        ):
            failures.append(
                f"{key}: candidate provenance is not synthetic/pending_human"
            )
        if item.get("reviewer") or item.get("reviewed_at") or item.get("review_note"):
            failures.append(f"{key}: pending candidate contains review approval")
        if not partition_version:
            missing_partition_versions.append(key)
        else:
            partition_version_ids.add(partition_version)
        difficulty = item.get("difficulty")
        if not isinstance(difficulty, int) or not 1 <= difficulty <= 3:
            failures.append(f"{key}: missing or invalid difficulty")
        constraints = item.get("constraints")
        if not isinstance(constraints, dict):
            failures.append(f"{key}: missing constraints")
        else:
            review = constraints.get("candidate_review")
            if not isinstance(review, dict) or review.get("status") != "pending_human":
                failures.append(f"{key}: candidate_review provenance is incomplete")
        if category == "unanswerable":
            if spans or answer is not None or tools:
                failures.append(f"{key}: unanswerable contract violated")
        elif category == "agent_task":
            if not tools:
                failures.append(f"{key}: agent_task lacks gold_tools")
            elif any(
                not isinstance(tool, dict)
                or not tool.get("name")
                or not isinstance(tool.get("arguments"), dict)
                for tool in tools
            ):
                failures.append(f"{key}: malformed agent_task gold_tools")
            elif any(not tool["arguments"] for tool in tools):
                empty_tool_arguments.append(key)
        elif category in ANSWERABLE_WITH_SPANS and (
            not spans or not isinstance(answer, str) or not answer.strip()
        ):
            failures.append(f"{key}: answerable retrieval item lacks span/answer")
        if (category == "temporal") != bool(item.get("temporal_ctx")):
            failures.append(f"{key}: temporal_ctx contract violated")
        if spans and answer == "\n\n".join(
            str(span.get("quote", "")) for span in spans
        ):
            raw_block_answers.append(key)
        if partition_version:
            version_splits[partition_version].add(split)
        for span in spans:
            version_id = str(span.get("version_id"))
            start = int(span.get("char_start", -1))
            end = int(span.get("char_end", -1))
            quote = str(span.get("quote", ""))
            version_splits[version_id].add(split)
            span_keys[(version_id, start, end)].append((split, key))
            span_records.append((version_id, start, end, quote, split, key))

    duplicate_questions = {
        question: keys
        for question, keys in normalized_questions.items()
        if len(keys) > 1
    }
    if duplicate_questions:
        failures.append(f"exact normalized duplicate questions: {duplicate_questions}")
    cross_split_versions = sorted(
        version_id for version_id, splits in version_splits.items() if len(splits) > 1
    )
    if cross_split_versions:
        failures.append(f"cross-split versions: {cross_split_versions}")
    cross_split_spans = sorted(
        key
        for key, usages in span_keys.items()
        if len({split for split, _ in usages}) > 1
    )
    if cross_split_spans:
        failures.append(f"cross-split spans: {cross_split_spans}")

    version_ids = sorted({record[0] for record in span_records} | partition_version_ids)
    statement = text(
        """
        SELECT v.id::text AS version_id, v.full_text, v.activated_at, v.invalid_at,
               b.block_idx, b.char_start, b.char_end, b.text
        FROM document_versions v
        LEFT JOIN parsed_blocks b ON b.version_id=v.id
        WHERE v.id IN :version_ids
        ORDER BY v.id, b.block_idx
        """
    ).bindparams(bindparam("version_ids", expanding=True))
    human_statement = text(
        """
        SELECT d.split, i.question, i.gold_spans
        FROM eval_items i JOIN eval_datasets d ON d.id=i.dataset_id
        WHERE d.name IN ('core-dev', 'english-dev') AND i.origin='human'
        """
    )
    async with session_factory() as session:
        rows = (
            (await session.execute(statement, {"version_ids": version_ids}))
            .mappings()
            .all()
        )
        human_rows = (await session.execute(human_statement)).mappings().all()
        await session.rollback()
    await close_database()
    blocks_by_version: dict[str, list[dict[str, Any]]] = defaultdict(list)
    full_text_by_version: dict[str, str] = {}
    inactive_versions: set[str] = set()
    for row in rows:
        version_id = str(row["version_id"])
        full_text_by_version[version_id] = str(row["full_text"])
        if row["activated_at"] is None or row["invalid_at"] is not None:
            inactive_versions.add(version_id)
        if row["block_idx"] is not None:
            blocks_by_version[version_id].append(dict(row))
    missing_versions = sorted(set(version_ids) - set(full_text_by_version))
    if missing_versions:
        failures.append(f"missing versions: {missing_versions}")
    if inactive_versions:
        failures.append(f"inactive versions: {sorted(inactive_versions)}")

    bad_quotes: list[str] = []
    outside_blocks: list[str] = []
    for version_id, start, end, quote, _split, key in span_records:
        full_text = full_text_by_version.get(version_id)
        if (
            full_text is None
            or start < 0
            or end <= start
            or full_text[start:end] != quote
        ):
            bad_quotes.append(key)
            continue
        containing = [
            block
            for block in blocks_by_version[version_id]
            if int(block["char_start"]) <= start <= end <= int(block["char_end"])
            and str(block["text"])[
                start - int(block["char_start"]) : end - int(block["char_start"])
            ]
            == quote
        ]
        if not containing:
            outside_blocks.append(key)
    if bad_quotes:
        failures.append(f"invalid full_text quote/range: {sorted(set(bad_quotes))}")
    if outside_blocks:
        failures.append(
            f"span not contained in parsed block: {sorted(set(outside_blocks))}"
        )

    human_questions = {_normalize_question(str(row["question"])) for row in human_rows}
    non_dev_human = sum(str(row["split"]) != "dev" for row in human_rows)
    if non_dev_human:
        failures.append(f"existing 40 human items outside dev split: {non_dev_human}")
    copied_questions = sorted(
        key
        for normalized, keys in normalized_questions.items()
        if normalized in human_questions
        for key in keys
    )
    if copied_questions:
        failures.append(f"questions copied from existing human set: {copied_questions}")
    human_versions = {
        str(span["version_id"])
        for row in human_rows
        for span in (row["gold_spans"] or [])
    }
    test_versions = {
        str(span["version_id"])
        for item in items
        if item.get("split") == "test"
        for span in (item.get("gold_spans") or [])
    }
    overlap_human_test = sorted(human_versions & test_versions)
    if overlap_human_test:
        failures.append(
            f"test versions overlap existing dev human versions: {overlap_human_test}"
        )

    if generic_questions:
        failures.append(f"generic template questions: {len(generic_questions)}")
    if missing_partition_versions:
        failures.append(
            f"missing partition_version_id: {len(missing_partition_versions)}"
        )
    if incomplete_schema:
        failures.append(f"incomplete review schema: {len(incomplete_schema)}")
    if placeholder_questions:
        failures.append(
            f"placeholder block labels in questions: {placeholder_questions}"
        )
    if raw_block_answers:
        failures.append(
            f"gold_answer is raw concatenated evidence block: {len(raw_block_answers)}"
        )
    if empty_tool_arguments:
        warnings.append(
            f"agent_task tools with empty arguments: {len(empty_tool_arguments)}"
        )
    repeated_skeletons = {key: count for key, count in skeletons.items() if count >= 3}
    if repeated_skeletons:
        warnings.append(f"question skeletons repeated >=3 times: {repeated_skeletons}")

    return {
        "status": "fail" if failures else "pass_with_warnings" if warnings else "pass",
        "item_count": len(items),
        "split_counts": dict(split_counts),
        "language_counts": {f"{s}:{l}": n for (s, l), n in language_counts.items()},
        "split_category_counts": {
            split: dict(counts) for split, counts in split_category_counts.items()
        },
        "span_count": len(span_records),
        "verified_quote_count": len(span_records) - len(bad_quotes),
        "verified_block_containment_count": len(span_records) - len(outside_blocks),
        "generic_question_count": len(generic_questions),
        "placeholder_question_keys": placeholder_questions,
        "missing_partition_version_count": len(missing_partition_versions),
        "incomplete_schema_count": len(incomplete_schema),
        "raw_block_answer_count": len(raw_block_answers),
        "failures": failures,
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(audit(args.output_dir)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
