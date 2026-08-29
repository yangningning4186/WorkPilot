"""通用的零模型、无外部 I/O pytest 契约。

这类 contract 把安全控制面的关键不变量提升为评测目录的一等资源。它不是模型
benchmark，也不冒充人工晋升的 baseline；nightly 只按这里固定的 pytest target
运行确定性回归。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "workpilot-deterministic-contract.v1"
MODE = "pytest_no_model_io"

_ID = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_TARGET = re.compile(r"backend/tests/[a-zA-Z0-9_./-]+\.py::test_[a-zA-Z0-9_]+\Z")


class DeterministicContractError(ValueError):
    """确定性契约不可无歧义、可复现地执行。"""


def load_contract(path: Path, *, repo_root: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeterministicContractError(f"contract 不是可读 UTF-8 JSON: {error}") from error
    if not isinstance(payload, dict):
        raise DeterministicContractError("contract 顶层必须是对象")
    return validate_contract(payload, repo_root=repo_root)


def validate_contract(
    payload: Mapping[str, Any],
    *,
    repo_root: Path,
) -> dict[str, Any]:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise DeterministicContractError(f"schema_version 必须是 {SCHEMA_VERSION}")
    contract_id = payload.get("id")
    if not isinstance(contract_id, str) or _ID.fullmatch(contract_id) is None:
        raise DeterministicContractError("contract.id 非法")
    if payload.get("mode") != MODE:
        raise DeterministicContractError(f"contract.mode 必须是 {MODE}")

    case_count = payload.get("case_count")
    categories = payload.get("categories")
    cases = payload.get("cases")
    if isinstance(case_count, bool) or not isinstance(case_count, int) or case_count < 1:
        raise DeterministicContractError("contract.case_count 必须是正整数")
    if not isinstance(categories, dict) or not categories:
        raise DeterministicContractError("contract.categories 必须是非空对象")
    if any(
        not isinstance(key, str)
        or not key
        or isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        for key, value in categories.items()
    ):
        raise DeterministicContractError("contract.categories 配额非法")
    if not isinstance(cases, list) or len(cases) != case_count:
        raise DeterministicContractError("contract case 数量漂移")
    if sum(categories.values()) != case_count:
        raise DeterministicContractError("contract category 配额之和必须等于 case_count")

    root = repo_root.resolve()
    ids: list[str] = []
    targets: list[str] = []
    actual_categories: Counter[str] = Counter()
    for case in cases:
        if not isinstance(case, Mapping):
            raise DeterministicContractError("contract case 必须是对象")
        case_id = case.get("id")
        category = case.get("category")
        target = case.get("test")
        invariants = case.get("invariants")
        if (
            not isinstance(case_id, str)
            or _ID.fullmatch(case_id) is None
            or not case_id.startswith(f"{contract_id}-")
        ):
            raise DeterministicContractError(f"contract case id 非法: {case_id!r}")
        if category not in categories:
            raise DeterministicContractError(f"{case_id}: contract category 非法")
        if not isinstance(target, str) or _TARGET.fullmatch(target) is None:
            raise DeterministicContractError(f"{case_id}: pytest target 非法")
        relative_file, test_name = target.split("::", 1)
        resolved = (root / relative_file).resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise DeterministicContractError(f"{case_id}: pytest target 文件不存在或逃出仓库")
        try:
            source = resolved.read_text(encoding="utf-8")
        except OSError as error:
            raise DeterministicContractError(f"{case_id}: pytest target 不可读: {error}") from error
        if re.search(rf"(?:async\s+)?def\s+{re.escape(test_name)}\s*\(", source) is None:
            raise DeterministicContractError(f"{case_id}: pytest target 函数不存在")
        if (
            not isinstance(invariants, list)
            or not invariants
            or any(not isinstance(value, str) or not value.strip() for value in invariants)
        ):
            raise DeterministicContractError(f"{case_id}: invariants 必须是非空字符串数组")
        ids.append(case_id)
        targets.append(target)
        actual_categories[str(category)] += 1

    if len(set(ids)) != len(ids) or len(set(targets)) != len(targets):
        raise DeterministicContractError("contract id/test target 不能重复")
    if dict(actual_categories) != categories:
        raise DeterministicContractError(f"contract category 配额漂移: {dict(actual_categories)}")
    return {
        "schema_version": SCHEMA_VERSION,
        "id": contract_id,
        "mode": MODE,
        "case_count": case_count,
        "categories": dict(sorted(categories.items())),
        "targets": targets,
    }
