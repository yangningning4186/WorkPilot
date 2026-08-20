"""Cowork 端到端标注任务集的静态校验与摘要。

任务集本身只描述可复现 fixture、能力边界和 gold；真实运行结果由后续 runner
记录为 observation。这里先 fail-closed，避免缺 gold、工具名漂移或 dev/test 配额
悄悄进入基线。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "cowork-task-suite.v1"
DEFAULT_SUITE = Path(__file__).parent / "suites" / "cowork-core-40.json"
EXPECTED_ITEMS = 40
EXPECTED_SPLITS = {"dev": 32, "test": 8}
EXPECTED_CATEGORIES = {
    "workspace": 8,
    "artifact": 8,
    "office": 6,
    "web": 6,
    "knowledge": 8,
    "safety_hitl": 4,
}

KNOWN_TOOLS = {
    "ask_user",
    "browser_close",
    "browser_find",
    "browser_open",
    "browser_snapshot",
    "create_artifact",
    "create_native_artifact",
    "edit_excel",
    "edit_word",
    "fetch_url",
    "inspect_office_file",
    "list_files",
    "list_office_files",
    "list_workspace_roots",
    "read_text_file",
    "request_capability",
    "request_directory",
    "run_shell",
    "search_files",
    "search_knowledge",
    "web_search",
    "write_text_file",
}

KNOWN_CAPABILITIES = {
    "filesystem.read",
    "filesystem.write",
    "network.read",
    "shell.execute",
    "office.word.edit",
    "office.excel.edit",
}

KNOWN_ASSERTIONS = {
    "artifact_registered",
    "baseline_used",
    "citation_url",
    "csv_rows_equal",
    "evidence_contract",
    "file_absent",
    "file_contains",
    "file_exists",
    "file_not_contains",
    "files_still_exist",
    "hitl_interrupt",
    "json_file_equals",
    "native_artifact_valid",
    "native_file_contains",
    "native_file_not_contains",
    "no_external_request_before_approval",
    "no_files_changed",
    "no_lost_update",
    "no_path_guessed",
    "no_private_network_content_exposed",
    "no_shell_effect_before_approval",
    "no_write_before_approval",
    "response_contains",
    "response_contains_any",
    "response_max_chars",
    "response_not_contains",
    "tool_error_expected",
    "tool_error_recovered",
    "xlsx_cells_equal",
}


class CoworkSuiteError(ValueError):
    """任务集违反冻结 schema。"""


def load_suite(path: Path = DEFAULT_SUITE) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CoworkSuiteError("suite 顶层必须是对象")
    validate_suite(payload)
    return payload


def _non_empty_strings(values: object, *, label: str) -> list[str]:
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value.strip() for value in values
    ):
        raise CoworkSuiteError(f"{label} 必须是非空字符串数组")
    return values


def _validate_tool_labels(item_id: str, gold: dict[str, Any]) -> None:
    required = _non_empty_strings(
        gold.get("required_tools"), label=f"{item_id}.gold.required_tools"
    )
    forbidden = _non_empty_strings(
        gold.get("forbidden_tools", []), label=f"{item_id}.gold.forbidden_tools"
    )
    unknown = (set(required) | set(forbidden)) - KNOWN_TOOLS
    if unknown:
        raise CoworkSuiteError(f"{item_id}: 未知工具 {sorted(unknown)}")
    overlap = set(required) & set(forbidden)
    if overlap:
        raise CoworkSuiteError(
            f"{item_id}: 工具同时 required/forbidden {sorted(overlap)}"
        )

    order = gold.get("required_tool_order", [])
    if not isinstance(order, list) or any(name not in required for name in order):
        raise CoworkSuiteError(
            f"{item_id}: required_tool_order 必须是 required_tools 子序列"
        )
    minimums = gold.get("minimum_tool_calls", {})
    if not isinstance(minimums, dict) or any(
        name not in required or not isinstance(count, int) or count < 1
        for name, count in minimums.items()
    ):
        raise CoworkSuiteError(f"{item_id}: minimum_tool_calls 非法")

    optimal = gold.get("optimal_tool_calls")
    maximum = gold.get("max_tool_calls")
    if (
        not isinstance(optimal, int)
        or not isinstance(maximum, int)
        or optimal < 1
        or maximum < optimal
    ):
        raise CoworkSuiteError(f"{item_id}: optimal/max_tool_calls 非法")


def validate_suite(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise CoworkSuiteError("schema_version 不匹配")
    if payload.get("origin") != "synthetic":
        raise CoworkSuiteError("未经 owner 复核的套件 origin 必须是 synthetic")
    if payload.get("review_status") != "pending_human_review":
        raise CoworkSuiteError("套件必须保持 pending_human_review")

    fixtures = payload.get("fixtures")
    if not isinstance(fixtures, dict) or not fixtures:
        raise CoworkSuiteError("fixtures 必须是非空对象")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != EXPECTED_ITEMS:
        raise CoworkSuiteError(f"任务必须恰好 {EXPECTED_ITEMS} 条")

    ids: list[str] = []
    prompts: list[str] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise CoworkSuiteError("item 必须是对象")
        item_id = raw.get("id")
        if not isinstance(item_id, str) or not item_id.startswith("cowork-core-"):
            raise CoworkSuiteError(f"非法 item id: {item_id!r}")
        ids.append(item_id)
        prompt = raw.get("prompt")
        if not isinstance(prompt, str) or len(prompt.strip()) < 8:
            raise CoworkSuiteError(f"{item_id}: prompt 过短")
        prompts.append(prompt.strip())
        if raw.get("split") not in EXPECTED_SPLITS:
            raise CoworkSuiteError(f"{item_id}: split 非法")
        if raw.get("category") not in EXPECTED_CATEGORIES:
            raise CoworkSuiteError(f"{item_id}: category 非法")
        if raw.get("difficulty") not in {1, 2, 3}:
            raise CoworkSuiteError(f"{item_id}: difficulty 非法")
        if (
            raw.get("origin") != "synthetic"
            or raw.get("review_status") != "pending_human"
        ):
            raise CoworkSuiteError(f"{item_id}: 标注 provenance 非法")

        fixture_ids = _non_empty_strings(
            raw.get("fixture_ids"), label=f"{item_id}.fixture_ids"
        )
        missing = set(fixture_ids) - set(fixtures)
        if missing:
            raise CoworkSuiteError(f"{item_id}: fixture 不存在 {sorted(missing)}")
        capabilities = _non_empty_strings(
            raw.get("granted_capabilities", []),
            label=f"{item_id}.granted_capabilities",
        )
        unknown_caps = set(capabilities) - KNOWN_CAPABILITIES
        if unknown_caps:
            raise CoworkSuiteError(f"{item_id}: capability 非法 {sorted(unknown_caps)}")

        gold = raw.get("gold")
        if not isinstance(gold, dict):
            raise CoworkSuiteError(f"{item_id}: 缺 gold")
        _validate_tool_labels(item_id, gold)
        if gold.get("expected_status") not in {"done", "waiting_human"}:
            raise CoworkSuiteError(f"{item_id}: expected_status 非法")
        assertions = gold.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise CoworkSuiteError(f"{item_id}: 至少需要一个 success assertion")
        for assertion in assertions:
            if not isinstance(assertion, dict) or not isinstance(
                assertion.get("type"), str
            ):
                raise CoworkSuiteError(f"{item_id}: assertion 非法")
            if assertion["type"] not in KNOWN_ASSERTIONS:
                raise CoworkSuiteError(
                    f"{item_id}: runner 不支持 assertion {assertion['type']!r}"
                )

        if raw["category"] == "knowledge":
            if "search_knowledge" not in gold["required_tools"]:
                raise CoworkSuiteError(
                    f"{item_id}: knowledge 任务必须调用 search_knowledge"
                )
            if not any(a.get("type") == "evidence_contract" for a in assertions):
                raise CoworkSuiteError(f"{item_id}: knowledge 任务缺 evidence_contract")

    if len(set(ids)) != len(ids):
        raise CoworkSuiteError("item id 重复")
    if len(set(prompts)) != len(prompts):
        raise CoworkSuiteError("prompt 重复")
    split_counts = Counter(str(item["split"]) for item in items)
    category_counts = Counter(str(item["category"]) for item in items)
    if dict(split_counts) != EXPECTED_SPLITS:
        raise CoworkSuiteError(f"split 配额漂移: {dict(split_counts)}")
    if dict(category_counts) != EXPECTED_CATEGORIES:
        raise CoworkSuiteError(f"category 配额漂移: {dict(category_counts)}")


def summarize_suite(payload: dict[str, Any]) -> dict[str, Any]:
    validate_suite(payload)
    items = payload["items"]
    return {
        "name": payload["name"],
        "version": payload["version"],
        "items": len(items),
        "splits": dict(sorted(Counter(item["split"] for item in items).items())),
        "categories": dict(sorted(Counter(item["category"] for item in items).items())),
        "difficulties": dict(
            sorted(Counter(str(item["difficulty"]) for item in items).items())
        ),
        "hitl_items": sum(
            item["gold"]["expected_status"] == "waiting_human" for item in items
        ),
        "average_optimal_tool_calls": round(
            sum(item["gold"]["optimal_tool_calls"] for item in items) / len(items), 2
        ),
        "review_status": payload["review_status"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="校验 Cowork 端到端标注任务集")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    args = parser.parse_args()
    print(
        json.dumps(
            summarize_suite(load_suite(args.suite)), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
