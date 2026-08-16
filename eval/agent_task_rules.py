"""agent_task 的确定性规则轨：工具、HITL、终态与产物必须同时成立。"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentTaskRuleResult:
    tool_sequence_match: bool
    tool_arguments_match: bool
    tool_status_ok: bool
    workflow_type_match: bool
    terminal_status_match: bool
    hitl_match: bool
    artifact_path_safe: bool
    artifact_hash_match: bool
    artifact_constraints_match: bool
    passed: bool

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


def _contains(actual: object, expected: object) -> bool:
    """gold arguments 是必要参数子集，不要求记录易漂移的完整中间对象。"""

    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _contains(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and actual == expected
    return actual == expected


def _safe_artifact(package: Path, relative_path: str) -> Path | None:
    root = package.resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def evaluate_agent_task(
    task: dict[str, Any], observed: dict[str, Any], *, package: Path
) -> AgentTaskRuleResult:
    gold_tools = list(task.get("gold_tools") or [])
    actual_tools = list(observed.get("tools") or [])
    gold_names = [item.get("name") for item in gold_tools]
    actual_names = [item.get("name") for item in actual_tools]
    sequence_match = gold_names == actual_names and bool(gold_names)
    arguments_match = sequence_match and all(
        _contains(actual.get("arguments"), gold.get("arguments"))
        for gold, actual in zip(gold_tools, actual_tools, strict=True)
    )
    statuses_ok = bool(actual_tools) and all(
        item.get("status") == "ok" for item in actual_tools
    )
    constraints = dict(task.get("constraints") or {})
    workflow_match = observed.get("workflow_type") == constraints.get(
        "expected_workflow_type"
    )
    terminal_match = observed.get("status") == "done"
    hitl_match = observed.get("hitl_decision") == constraints.get("hitl_decision")

    relative_path = observed.get("artifact_path")
    artifact = (
        _safe_artifact(package, relative_path) if isinstance(relative_path, str) else None
    )
    path_safe = artifact is not None
    actual_sha = hashlib.sha256(artifact.read_bytes()).hexdigest() if artifact else None
    expected_sha = constraints.get("expected_artifact_sha256")
    hash_match = (
        path_safe
        and isinstance(expected_sha, str)
        and actual_sha == expected_sha
        and observed.get("artifact_sha256") == expected_sha
    )
    text = artifact.read_text(encoding="utf-8") if artifact else ""
    must_include = list(constraints.get("must_include") or [])
    must_not_include = list(constraints.get("must_not_include") or [])
    content_match = bool(must_include) and all(
        isinstance(term, str) and term in text for term in must_include
    ) and all(isinstance(term, str) and term not in text for term in must_not_include)

    checks = (
        sequence_match,
        arguments_match,
        statuses_ok,
        workflow_match,
        terminal_match,
        hitl_match,
        path_safe,
        hash_match,
        content_match,
    )
    return AgentTaskRuleResult(*checks, passed=all(checks))
