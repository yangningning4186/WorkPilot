"""Cowork 端到端标注任务集的静态校验与摘要。

任务集本身只描述可复现 fixture、能力边界和 gold；真实运行结果由后续 runner
记录为 observation。这里先 fail-closed，避免缺 gold、工具名漂移或 dev/test 配额
悄悄进入基线。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from app.cowork.connector_tools import register_connector_tools
from app.cowork.tools import build_default_cowork_registry
from app.cowork_policy import ACTIVE_CAPABILITIES

SCHEMA_VERSION = "cowork-task-suite.v1"
DEFAULT_SUITE = Path(__file__).parent / "suites" / "cowork-core-50.json"
EXPECTED_ITEMS = 50
EXPECTED_SPLITS = {"dev": 39, "test": 11}
EXPECTED_CATEGORIES = {
    # 只读 git 视图（git_status / git_diff / git_log）与「读长文件后做局部替换」
    # 都归在 workspace 下：它们回答的是同一类问题——在用户的目录里看清现状再动手。
    "workspace": 12,
    "artifact": 8,
    "office": 6,
    "web": 6,
    "knowledge": 8,
    # 工作区文档的沉浸阅读：locator 寻址、读过再引、不知道就说不知道。和 knowledge 分开，
    # 因为那一类考的是跨文档检索，这一类考的是单份文档内的定位与引用可回溯。
    "reading": 4,
    "safety_hitl": 4,
    # 中文办公栈的一等能力：飞书专用工具与会话级 PTY 都需要独立于通用工作区题覆盖。
    "connector": 1,
    "shell": 1,
}

# 工具名与 capability 都从产品注册表现取, 不再手抄。
# 手抄过一次的代价已经付了: 注册表给 search_knowledge 加上 knowledge.read 之后,
# 这份名单没跟上, 八条 knowledge 任务在"尚未授予 knowledge.read"上全灭了一整轮,
# 而套件校验一声不吭——它照的是自己那份过期的镜子。
_REGISTRY = build_default_cowork_registry()
register_connector_tools(_REGISTRY)

# 只有需要运行期对象(RAG service、浏览器管理器)才注册的工具进不了默认注册表,
# 它们的 capability 在这里显式声明。这份声明**允许**过期, 因为它不是最后一道关:
# runner 会拿真正要跑的那个 registry 再核一遍(_assert_item_is_solvable), 那一遍
# 按定义不可能漂移。这里只负责让"改错工具名"在不花模型调用的前提下就被拦住。
_ADAPTER_TOOL_CAPABILITIES: dict[str, frozenset[str]] = {
    # Skill 工具依赖运行期 catalog，所以和 RAG / 浏览器一样不在默认注册表里。
    "list_skills": frozenset(),
    "load_skill": frozenset(),
    "load_skill_resource": frozenset(),
    "search_knowledge": frozenset({"knowledge.read"}),
    "browser_open": frozenset({"network.fetch", "browser.read"}),
    "browser_snapshot": frozenset({"browser.read"}),
    "browser_find": frozenset({"browser.read"}),
    "browser_close": frozenset({"browser.read"}),
}

# 退役工具只能出现在 forbidden_tools 中，用来冻结“不得回退旧协议”的产品契约；
# required_tools 若引用这些名字仍会 fail closed。
RETIRED_TOOLS = frozenset(
    {
        "create_native_artifact",
        "list_office_files",
        "inspect_office_file",
        "edit_word",
        "edit_excel",
        "edit_office_file",
    }
)

KNOWN_CAPABILITIES = frozenset(ACTIVE_CAPABILITIES)
KNOWN_TOOLS = _REGISTRY.names() | frozenset(_ADAPTER_TOOL_CAPABILITIES)
# 工具执行前会校验的 capability 全集, 用来判断题目给的授权够不够跑通它自己的 gold。
TOOL_CAPABILITIES: dict[str, frozenset[str]] = {
    **_ADAPTER_TOOL_CAPABILITIES,
    **{
        name: frozenset(
            ({_REGISTRY.get(name).capability} - {None})
            | set(_REGISTRY.get(name).extra_capabilities)
        )
        for name in _REGISTRY.names()
    },
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
    "response_refusal_before_claim",
    "tool_error_expected",
    "tool_error_recovered",
    "xlsx_cells_equal",
}


class CoworkSuiteError(ValueError):
    """任务集违反冻结 schema。"""


def suite_review(payload: Mapping[str, Any]) -> dict[str, str | None]:
    """返回可审计的套件复核状态，并拒绝半批准状态。"""

    status = payload.get("review_status")
    if status not in {"pending_human_review", "approved"}:
        raise CoworkSuiteError("review_status 必须是 pending_human_review 或 approved")
    reviewer = payload.get("reviewer")
    reviewed_at = payload.get("reviewed_at")
    if status == "pending_human_review":
        if reviewer is not None or reviewed_at is not None:
            raise CoworkSuiteError("pending_human_review 不能提前填写 reviewer/reviewed_at")
        return {"status": status, "reviewer": None, "reviewed_at": None}
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise CoworkSuiteError("approved 套件必须填写 reviewer")
    if not isinstance(reviewed_at, str) or not reviewed_at.strip():
        raise CoworkSuiteError("approved 套件必须填写 reviewed_at")
    try:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise CoworkSuiteError("reviewed_at 必须是 ISO-8601 时间") from error
    if parsed.tzinfo is None:
        raise CoworkSuiteError("reviewed_at 必须包含时区")
    return {
        "status": status,
        "reviewer": reviewer.strip(),
        "reviewed_at": reviewed_at.strip(),
    }


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


def missing_capabilities_for(
    required_tools: Iterable[str],
    granted: Iterable[str],
    *,
    tool_capabilities: Mapping[str, frozenset[str]] = TOOL_CAPABILITIES,
) -> dict[str, list[str]]:
    """gold 要求调用的工具里, 哪些的 capability 这道题根本没授权。

    这类题目不是"难", 是**不可解**: 工具执行入口会先拒掉, 模型只能转去
    request_capability, run 停在 waiting_human, 于是这一分永远拿不到。把它算进
    成功率等于在给评分注入一个与被测系统无关的常数项。
    """

    available = set(granted)
    gaps: dict[str, list[str]] = {}
    for name in required_tools:
        needed = tool_capabilities.get(name)
        if needed is None:
            continue
        lacking = sorted(needed - available)
        if lacking:
            gaps[name] = lacking
    return gaps


def _validate_tool_labels(item_id: str, gold: dict[str, Any]) -> None:
    required = _non_empty_strings(
        gold.get("required_tools"), label=f"{item_id}.gold.required_tools"
    )
    forbidden = _non_empty_strings(
        gold.get("forbidden_tools", []), label=f"{item_id}.gold.forbidden_tools"
    )
    unknown_required = set(required) - KNOWN_TOOLS
    unknown_forbidden = set(forbidden) - KNOWN_TOOLS - RETIRED_TOOLS
    if unknown_required or unknown_forbidden:
        raise CoworkSuiteError(
            f"{item_id}: 未知工具 {sorted(unknown_required | unknown_forbidden)}"
        )
    overlap = set(required) & set(forbidden)
    if overlap:
        raise CoworkSuiteError(f"{item_id}: 工具同时 required/forbidden {sorted(overlap)}")

    order = gold.get("required_tool_order", [])
    if not isinstance(order, list) or any(name not in required for name in order):
        raise CoworkSuiteError(f"{item_id}: required_tool_order 必须是 required_tools 子序列")
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
        raise CoworkSuiteError("origin 必须保留题目生成来源 synthetic")
    review = suite_review(payload)

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
        expected_item_review = (
            "approved" if review["status"] == "approved" else "pending_human"
        )
        if raw.get("origin") != "synthetic" or raw.get("review_status") != expected_item_review:
            raise CoworkSuiteError(f"{item_id}: 标注 provenance 非法")

        fixture_ids = _non_empty_strings(raw.get("fixture_ids"), label=f"{item_id}.fixture_ids")
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
        gaps = missing_capabilities_for(gold.get("required_tools", []), capabilities)
        if gaps:
            detail = "; ".join(f"{name} 需要 {caps}" for name, caps in sorted(gaps.items()))
            raise CoworkSuiteError(
                f"{item_id}: granted_capabilities 不足以跑通自己的 gold（{detail}）"
            )
        if gold.get("expected_status") not in {"done", "waiting_human"}:
            raise CoworkSuiteError(f"{item_id}: expected_status 非法")
        assertions = gold.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise CoworkSuiteError(f"{item_id}: 至少需要一个 success assertion")
        for assertion in assertions:
            if not isinstance(assertion, dict) or not isinstance(assertion.get("type"), str):
                raise CoworkSuiteError(f"{item_id}: assertion 非法")
            if assertion["type"] not in KNOWN_ASSERTIONS:
                raise CoworkSuiteError(f"{item_id}: runner 不支持 assertion {assertion['type']!r}")

        if raw["category"] == "knowledge":
            if "search_knowledge" not in gold["required_tools"]:
                raise CoworkSuiteError(f"{item_id}: knowledge 任务必须调用 search_knowledge")
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
    review = suite_review(payload)
    return {
        "name": payload["name"],
        "version": payload["version"],
        "items": len(items),
        "splits": dict(sorted(Counter(item["split"] for item in items).items())),
        "categories": dict(sorted(Counter(item["category"] for item in items).items())),
        "difficulties": dict(sorted(Counter(str(item["difficulty"]) for item in items).items())),
        "hitl_items": sum(item["gold"]["expected_status"] == "waiting_human" for item in items),
        "average_optimal_tool_calls": round(
            sum(item["gold"]["optimal_tool_calls"] for item in items) / len(items), 2
        ),
        "review_status": review["status"],
        "reviewer": review["reviewer"],
        "reviewed_at": review["reviewed_at"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="校验 Cowork 端到端标注任务集")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    args = parser.parse_args()
    print(json.dumps(summarize_suite(load_suite(args.suite)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
