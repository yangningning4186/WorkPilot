"""版本化评测目录与只读健康检查。

``doctor`` 只检查可复现评测所依赖的静态契约，不运行模型、工具或副作用。目录中的
所有路径都相对于仓库根目录解析；绝对路径、``..`` 逃逸和指向仓库外的符号链接都会
被拒绝。

退出码是稳定协议：0=目录结构健康（可以包含需要重建 baseline 的 warning），
2=目录配置损坏。``rebuild_required`` 是有意暴露的迁移状态，不会被伪装成 ready。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

CATALOG_SCHEMA_VERSION = "workpilot-eval-catalog.v1"
POLICY_SCHEMA_VERSION = "workpilot-regression-policy.v1"
BASELINE_SCHEMA_VERSION = "workpilot-regression-baseline.v1"
REPLAY_SCHEMA = "workpilot.run-replay-bundle"
REPLAY_SCHEMA_VERSION = 1
REFUSAL_CALIBRATION_SCHEMA = "workpilot-refusal-calibration.v1"

DEFAULT_CATALOG = Path(__file__).resolve().with_name("catalog.json")
DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent

_RESOURCE_ID = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_TRACK_KINDS = frozenset({"cowork", "retrieval", "generation"})
_BASELINE_STATUSES = frozenset({"ready", "rebuild_required"})

Severity = Literal["error", "warning"]
ResourceType = Literal["track", "replay"]
ResourceHealth = Literal["ready", "warning", "invalid"]


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _is_git_sha(value: object) -> bool:
    return (
        isinstance(value, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is not None
    )


class DuplicateJsonKeyError(ValueError):
    """JSON 对象包含重复键，因而无法被无歧义地审计。"""


@dataclass(frozen=True)
class CatalogIssue:
    severity: Severity
    code: str
    message: str
    resource_id: str | None = None

    def to_dict(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True)
class CatalogResource:
    resource_type: ResourceType
    resource_id: str
    health: ResourceHealth
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class CatalogReport:
    catalog: str
    repo_root: str
    schema_version: str | None
    resources: tuple[CatalogResource, ...]
    issues: tuple[CatalogIssue, ...]

    @property
    def healthy(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def status(self) -> ResourceHealth:
        if not self.healthy:
            return "invalid"
        if any(issue.severity == "warning" for issue in self.issues):
            return "warning"
        return "ready"

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_version": 1,
            "catalog": self.catalog,
            "repo_root": self.repo_root,
            "schema_version": self.schema_version,
            "healthy": self.healthy,
            "status": self.status,
            "summary": {
                "resource_count": len(self.resources),
                "ready": sum(resource.health == "ready" for resource in self.resources),
                "warnings": sum(resource.health == "warning" for resource in self.resources),
                "invalid": sum(resource.health == "invalid" for resource in self.resources),
                "error_count": sum(issue.severity == "error" for issue in self.issues),
                "warning_count": sum(issue.severity == "warning" for issue in self.issues),
            },
            "resources": [resource.to_dict() for resource in self.resources],
            "issues": [issue.to_dict() for issue in self.issues],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    def to_text(self) -> str:
        lines = [
            f"Catalog doctor: {self.status.upper()}",
            f"catalog: {self.catalog}",
            f"schema: {self.schema_version or '<invalid>'}",
            "",
        ]
        for resource in self.resources:
            lines.append(
                f"{resource.health.upper():7} {resource.resource_type}:{resource.resource_id} "
                f"- {resource.detail}"
            )
        if self.issues:
            lines.extend(["", "Issues:"])
            for issue in self.issues:
                owner = f" [{issue.resource_id}]" if issue.resource_id is not None else ""
                lines.append(f"{issue.severity.upper():7}{owner} {issue.code}: {issue.message}")
        lines.extend(
            [
                "",
                (
                    "Result: structure healthy"
                    if self.healthy
                    else "Result: invalid catalog configuration"
                ),
            ]
        )
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class _ResolvedJson:
    relative: str
    path: Path
    payload: object
    raw_bytes: bytes


@dataclass(frozen=True)
class _TrackSelection:
    split: str
    item_count: int
    case_ids: frozenset[str]

    @property
    def split_counts(self) -> dict[str, int]:
        return {self.split: self.item_count}


def canonical_json(value: object) -> str:
    """与回归 baseline 相同的规范 JSON 口径。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"重复 JSON key: {key}")
        result[key] = value
    return result


def _decode_json(raw_bytes: bytes, *, label: str) -> object:
    try:
        return json.loads(raw_bytes, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKeyError) as error:
        raise ValueError(f"{label} 不是无歧义 UTF-8 JSON: {error}") from error


def _resource_id(value: object, *, fallback: str) -> tuple[str, CatalogIssue | None]:
    if isinstance(value, str) and _RESOURCE_ID.fullmatch(value):
        return value, None
    return fallback, CatalogIssue(
        "error",
        "resource_id_invalid",
        "id 必须匹配 [a-z0-9][a-z0-9._-]*",
        fallback,
    )


def _resolve_json(
    value: object,
    *,
    field: str,
    resource_id: str,
    repo_root: Path,
    issues: list[CatalogIssue],
) -> _ResolvedJson | None:
    if not isinstance(value, str) or not value.strip():
        issues.append(
            CatalogIssue("error", "path_invalid", f"{field} 必须是非空相对路径", resource_id)
        )
        return None
    relative = Path(value)
    if relative.is_absolute():
        issues.append(
            CatalogIssue(
                "error",
                "path_absolute",
                f"{field} 不允许绝对路径: {value}",
                resource_id,
            )
        )
        return None
    root = repo_root.resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        issues.append(
            CatalogIssue(
                "error",
                "path_escape",
                f"{field} 逃出仓库根目录: {value}",
                resource_id,
            )
        )
        return None
    if not resolved.is_file():
        issues.append(
            CatalogIssue(
                "error",
                "path_missing",
                f"{field} 文件不存在: {value}",
                resource_id,
            )
        )
        return None
    try:
        raw_bytes = resolved.read_bytes()
        payload = _decode_json(raw_bytes, label=value)
    except (OSError, ValueError) as error:
        issues.append(CatalogIssue("error", "json_invalid", str(error), resource_id))
        return None
    return _ResolvedJson(value, resolved, payload, raw_bytes)


def _validate_policy(
    document: _ResolvedJson | None,
    *,
    kind: str,
    resource_id: str,
    issues: list[CatalogIssue],
) -> Mapping[str, object] | None:
    if document is None:
        return None
    if not isinstance(document.payload, Mapping):
        issues.append(
            CatalogIssue("error", "policy_not_object", "policy 根节点必须是对象", resource_id)
        )
        return None
    policy = document.payload
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        issues.append(
            CatalogIssue(
                "error",
                "policy_schema_invalid",
                f"policy.schema_version 必须是 {POLICY_SCHEMA_VERSION}",
                resource_id,
            )
        )
    if policy.get("report_kind") != kind:
        issues.append(
            CatalogIssue(
                "error",
                "policy_kind_mismatch",
                f"track kind={kind!r}，policy report_kind={policy.get('report_kind')!r}",
                resource_id,
            )
        )
    if not isinstance(policy.get("name"), str) or not policy["name"]:
        issues.append(
            CatalogIssue(
                "error",
                "policy_name_invalid",
                "policy.name 必须是非空字符串",
                resource_id,
            )
        )
    return policy


def _suite_review(payload: Mapping[str, object]) -> dict[str, object]:
    nested = payload.get("review")
    review = nested if isinstance(nested, Mapping) else {}
    return {
        "origin": payload.get("origin"),
        "status": payload.get("review_status") or review.get("status"),
        "reviewer": payload.get("reviewer") or review.get("reviewer"),
        "reviewed_at": payload.get("reviewed_at") or review.get("reviewed_at"),
    }


def _valid_reviewed_at(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _validate_track_selection(
    raw: Mapping[str, object],
    *,
    suite: _ResolvedJson | None,
    resource_id: str,
    issues: list[CatalogIssue],
) -> _TrackSelection | None:
    value = raw.get("selection")
    if not isinstance(value, Mapping):
        issues.append(
            CatalogIssue(
                "error",
                "track_selection_invalid",
                "track 必须显式声明 selection.split 和 selection.item_count",
                resource_id,
            )
        )
        return None
    split = value.get("split")
    item_count = value.get("item_count")
    if split not in {"dev", "test"} or (
        isinstance(item_count, bool) or not isinstance(item_count, int) or item_count < 1
    ):
        issues.append(
            CatalogIssue(
                "error",
                "track_selection_invalid",
                "selection.split 必须是 dev/test，item_count 必须是正整数",
                resource_id,
            )
        )
        return None
    case_ids: set[str] = set()
    if suite is not None and isinstance(suite.payload, Mapping):
        items = suite.payload.get("items")
        if isinstance(items, list):
            selected = [
                item
                for item in items
                if isinstance(item, Mapping) and item.get("split") == split
            ]
            if len(selected) != item_count:
                issues.append(
                    CatalogIssue(
                        "error",
                        "track_selection_mismatch",
                        f"selection 声明 {split}={item_count}，suite 实际为 {len(selected)}",
                        resource_id,
                    )
                )
            for item in selected:
                case_id = item.get("id") or item.get("item_id")
                if isinstance(case_id, str) and case_id:
                    if case_id in case_ids:
                        issues.append(
                            CatalogIssue(
                                "error",
                                "suite_case_id_duplicate",
                                f"suite 选择内存在重复 case id: {case_id}",
                                resource_id,
                            )
                        )
                    case_ids.add(case_id)
        else:
            declared_count = suite.payload.get("item_count")
            if isinstance(declared_count, int) and declared_count != item_count:
                issues.append(
                    CatalogIssue(
                        "error",
                        "track_selection_mismatch",
                        f"selection.item_count={item_count}，suite.item_count={declared_count}",
                        resource_id,
                    )
                )
    return _TrackSelection(str(split), int(item_count), frozenset(case_ids))


def _gold_document_hashes(document: _ResolvedJson | None) -> set[str]:
    if document is None or not isinstance(document.payload, Mapping):
        return set()
    items = document.payload.get("items")
    if not isinstance(items, list):
        return set()
    hashes: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        groups = item.get("gold_evidence_groups")
        if not isinstance(groups, list):
            continue
        for group in groups:
            alternatives = group.get("alternatives") if isinstance(group, Mapping) else None
            if not isinstance(alternatives, list):
                continue
            for alternative in alternatives:
                content_hash = (
                    alternative.get("content_hash")
                    if isinstance(alternative, Mapping)
                    else None
                )
                if isinstance(content_hash, str):
                    hashes.add(content_hash)
    return hashes


def _validate_retrieval_calibration(
    value: object,
    *,
    evaluation_suite: _ResolvedJson | None,
    baseline_ready: bool,
    resource_id: str,
    repo_root: Path,
    issues: list[CatalogIssue],
) -> _ResolvedJson | None:
    if not isinstance(value, Mapping):
        issues.append(
            CatalogIssue(
                "error",
                "calibration_declaration_invalid",
                "retrieval track 必须声明独立 calibration suite 与状态",
                resource_id,
            )
        )
        return None
    status = value.get("status")
    if status not in _BASELINE_STATUSES:
        issues.append(
            CatalogIssue(
                "error",
                "calibration_status_invalid",
                f"calibration.status 必须是 {sorted(_BASELINE_STATUSES)} 之一",
                resource_id,
            )
        )
    calibration_suite = _resolve_json(
        value.get("suite"),
        field="calibration.suite",
        resource_id=resource_id,
        repo_root=repo_root,
        issues=issues,
    )
    if calibration_suite is not None and not isinstance(calibration_suite.payload, Mapping):
        issues.append(
            CatalogIssue(
                "error",
                "calibration_suite_invalid",
                "calibration suite 根节点必须是对象",
                resource_id,
            )
        )
    overlap = _gold_document_hashes(evaluation_suite) & _gold_document_hashes(calibration_suite)
    if overlap:
        issues.append(
            CatalogIssue(
                "error",
                "calibration_suite_leakage",
                f"calibration/evaluation gold 共享 {len(overlap)} 个证据文档",
                resource_id,
            )
        )
    if status == "rebuild_required":
        reason = value.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            issues.append(
                CatalogIssue(
                    "error",
                    "calibration_rebuild_reason_missing",
                    "calibration rebuild_required 必须提供非空 reason",
                    resource_id,
                )
            )
        if baseline_ready:
            issues.append(
                CatalogIssue(
                    "error",
                    "calibration_not_ready",
                    "retrieval baseline ready 前 calibration 必须先 ready",
                    resource_id,
                )
            )
        return None
    if status != "ready":
        return None
    artifact = _resolve_json(
        value.get("path"),
        field="calibration.path",
        resource_id=resource_id,
        repo_root=repo_root,
        issues=issues,
    )
    if artifact is None or not isinstance(artifact.payload, Mapping):
        if artifact is not None:
            issues.append(
                CatalogIssue(
                    "error",
                    "calibration_artifact_invalid",
                    "calibration artifact 根节点必须是对象",
                    resource_id,
                )
            )
        return artifact
    payload = artifact.payload
    suite_review = (
        _suite_review(calibration_suite.payload)
        if calibration_suite is not None and isinstance(calibration_suite.payload, Mapping)
        else {}
    )
    if (
        suite_review.get("status") != "approved"
        or not isinstance(suite_review.get("reviewer"), str)
        or not _valid_reviewed_at(suite_review.get("reviewed_at"))
    ):
        issues.append(
            CatalogIssue(
                "error",
                "calibration_suite_not_approved",
                "ready calibration suite 必须 approved 并包含完整复核信息",
                resource_id,
            )
        )
    expected_suite_sha = (
        hashlib.sha256(calibration_suite.raw_bytes).hexdigest()
        if calibration_suite is not None
        else None
    )
    if (
        payload.get("schema_version") != REFUSAL_CALIBRATION_SCHEMA
        or payload.get("dataset_sha256") != expected_suite_sha
        or not isinstance(payload.get("reviewer"), str)
        or not _valid_reviewed_at(payload.get("reviewed_at"))
    ):
        issues.append(
            CatalogIssue(
                "error",
                "calibration_artifact_invalid",
                "calibration artifact 的 schema/suite SHA/reviewer/reviewed_at 无效",
                resource_id,
            )
        )
    integrity = payload.get("integrity")
    unsigned = {key: item for key, item in payload.items() if key != "integrity"}
    if (
        not isinstance(integrity, Mapping)
        or integrity.get("algorithm") != "sha256"
        or integrity.get("value")
        != hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
    ):
        issues.append(
            CatalogIssue(
                "error",
                "calibration_integrity_invalid",
                "calibration artifact 完整性校验失败",
                resource_id,
            )
        )
    return artifact


def _validate_ready_baseline(
    document: _ResolvedJson | None,
    *,
    kind: str,
    suite: _ResolvedJson | None,
    policy_document: _ResolvedJson | None,
    policy: Mapping[str, object] | None,
    selection: _TrackSelection | None,
    calibration_artifact: _ResolvedJson | None,
    resource_id: str,
    issues: list[CatalogIssue],
) -> None:
    if document is None:
        return
    if not isinstance(document.payload, Mapping):
        issues.append(
            CatalogIssue("error", "baseline_not_object", "ready baseline 必须是对象", resource_id)
        )
        return
    baseline = document.payload
    if baseline.get("schema_version") != BASELINE_SCHEMA_VERSION:
        issues.append(
            CatalogIssue(
                "error",
                "baseline_schema_invalid",
                f"ready baseline 必须是 {BASELINE_SCHEMA_VERSION}",
                resource_id,
            )
        )
    if baseline.get("kind") != kind:
        issues.append(
            CatalogIssue(
                "error",
                "baseline_kind_mismatch",
                f"track kind={kind!r}，baseline kind={baseline.get('kind')!r}",
                resource_id,
            )
        )
    suite_name = suite.payload.get("name") if suite and isinstance(suite.payload, Mapping) else None
    if isinstance(suite_name, str) and baseline.get("dataset") != suite_name:
        issues.append(
            CatalogIssue(
                "error",
                "baseline_suite_mismatch",
                f"suite name={suite_name!r}，baseline dataset={baseline.get('dataset')!r}",
                resource_id,
            )
        )
    if suite is not None:
        actual_suite_sha = hashlib.sha256(suite.raw_bytes).hexdigest()
        if baseline.get("dataset_fingerprint") != actual_suite_sha:
            issues.append(
                CatalogIssue(
                    "error",
                    "baseline_suite_hash_mismatch",
                    "baseline.dataset_fingerprint 与当前 suite 文件 SHA256 不一致",
                    resource_id,
                )
            )
    for field in ("generated_at", "label"):
        if not isinstance(baseline.get(field), str) or not str(baseline[field]).strip():
            issues.append(
                CatalogIssue(
                    "error",
                    "baseline_provenance_missing",
                    f"ready baseline.{field} 必须是非空字符串",
                    resource_id,
                )
            )
    for field in ("dataset_fingerprint", "config_fingerprint", "source_report_sha256"):
        if not _is_sha256(baseline.get(field)):
            issues.append(
                CatalogIssue(
                    "error",
                    "baseline_provenance_invalid",
                    f"ready baseline.{field} 必须是 64 位小写 SHA256",
                    resource_id,
                )
            )
    if not _is_git_sha(baseline.get("git_sha")):
        issues.append(
            CatalogIssue(
                "error",
                "baseline_provenance_invalid",
                "ready baseline.git_sha 必须是 40 或 64 位小写 Git SHA",
                resource_id,
            )
        )
    if baseline.get("git_dirty") is not False:
        issues.append(
            CatalogIssue(
                "error",
                "baseline_git_dirty",
                "ready baseline.git_dirty 必须明确为 false",
                resource_id,
            )
        )
    suite_review = (
        _suite_review(suite.payload)
        if suite is not None and isinstance(suite.payload, Mapping)
        else {}
    )
    baseline_review = baseline.get("review")
    if (
        suite_review.get("status") != "approved"
        or not isinstance(suite_review.get("reviewer"), str)
        or not _valid_reviewed_at(suite_review.get("reviewed_at"))
    ):
        issues.append(
            CatalogIssue(
                "error",
                "suite_review_not_approved",
                "ready track 的 suite 必须 approved，并有 reviewer 和带时区的 reviewed_at",
                resource_id,
            )
        )
    if not isinstance(baseline_review, Mapping) or any(
        baseline_review.get(field) != suite_review.get(field)
        for field in ("origin", "status", "reviewer", "reviewed_at")
    ):
        issues.append(
            CatalogIssue(
                "error",
                "baseline_review_mismatch",
                "baseline 固定的 review provenance 与当前 suite 不一致",
                resource_id,
            )
        )
    if kind == "retrieval" and not _is_sha256(baseline.get("calibration_fingerprint")):
        issues.append(
            CatalogIssue(
                "error",
                "baseline_calibration_invalid",
                "ready retrieval baseline 必须固定独立校准文件 SHA256",
                resource_id,
            )
        )
    if kind == "retrieval" and calibration_artifact is not None:
        expected_calibration_sha = hashlib.sha256(calibration_artifact.raw_bytes).hexdigest()
        if baseline.get("calibration_fingerprint") != expected_calibration_sha:
            issues.append(
                CatalogIssue(
                    "error",
                    "baseline_calibration_mismatch",
                    "baseline 固定的 calibration SHA256 与 catalog artifact 不一致",
                    resource_id,
                )
            )
    baseline_policy = baseline.get("policy")
    if not isinstance(baseline_policy, Mapping):
        issues.append(
            CatalogIssue(
                "error",
                "baseline_policy_invalid",
                "ready baseline 缺少 policy 固定信息",
                resource_id,
            )
        )
    elif policy is not None and policy_document is not None:
        expected_hash = hashlib.sha256(
            canonical_json(policy_document.payload).encode("utf-8")
        ).hexdigest()
        if baseline_policy.get("name") != policy.get("name"):
            issues.append(
                CatalogIssue(
                    "error",
                    "baseline_policy_name_mismatch",
                    "baseline 固定的 policy.name 与当前 policy 不一致",
                    resource_id,
                )
            )
        if baseline_policy.get("sha256") != expected_hash:
            issues.append(
                CatalogIssue(
                    "error",
                    "baseline_policy_hash_mismatch",
                    "baseline 固定的 policy.sha256 与当前 policy 文件不一致",
                    resource_id,
                )
            )
    cases = baseline.get("cases")
    if not isinstance(cases, list) or not cases:
        issues.append(
            CatalogIssue(
                "error",
                "baseline_cases_invalid",
                "ready baseline.cases 必须是非空数组",
                resource_id,
            )
        )
    elif selection is not None:
        baseline_selection = baseline.get("selection")
        split_counts = (
            baseline_selection.get("split_counts")
            if isinstance(baseline_selection, Mapping)
            else None
        )
        if split_counts != selection.split_counts or len(cases) != selection.item_count:
            issues.append(
                CatalogIssue(
                    "error",
                    "baseline_selection_mismatch",
                    "baseline 的 split/count 与 catalog track selection 不一致",
                    resource_id,
                )
            )
        if selection.case_ids:
            baseline_ids = {
                case.get("case_id")
                for case in cases
                if isinstance(case, Mapping) and isinstance(case.get("case_id"), str)
            }
            if baseline_ids != set(selection.case_ids):
                issues.append(
                    CatalogIssue(
                        "error",
                        "baseline_case_ids_mismatch",
                        "baseline case_id 集合与 suite 所选 split 不一致",
                        resource_id,
                    )
                )
    integrity = baseline.get("integrity")
    if not isinstance(integrity, Mapping) or integrity.get("algorithm") != "sha256":
        issues.append(
            CatalogIssue(
                "error",
                "baseline_integrity_invalid",
                "ready baseline 缺少 sha256 完整性信息",
                resource_id,
            )
        )
    else:
        body = {key: value for key, value in baseline.items() if key != "integrity"}
        try:
            actual = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        except (TypeError, ValueError) as error:
            issues.append(
                CatalogIssue(
                    "error",
                    "baseline_integrity_invalid",
                    f"baseline 无法规范化: {error}",
                    resource_id,
                )
            )
        else:
            if integrity.get("value") != actual:
                issues.append(
                    CatalogIssue(
                        "error",
                        "baseline_integrity_mismatch",
                        "ready baseline 的完整性摘要不匹配",
                        resource_id,
                    )
                )


def _validate_track(
    raw: object,
    *,
    index: int,
    repo_root: Path,
    issues: list[CatalogIssue],
) -> tuple[str, str]:
    fallback = f"<track-{index}>"
    if not isinstance(raw, Mapping):
        issues.append(CatalogIssue("error", "track_not_object", "track 必须是对象", fallback))
        return fallback, "invalid track entry"
    resource_id, id_issue = _resource_id(raw.get("id"), fallback=fallback)
    if id_issue is not None:
        issues.append(id_issue)
    kind_value = raw.get("kind")
    kind = kind_value if isinstance(kind_value, str) else ""
    if kind not in _TRACK_KINDS:
        issues.append(
            CatalogIssue(
                "error",
                "track_kind_invalid",
                f"kind 必须是 {sorted(_TRACK_KINDS)} 之一",
                resource_id,
            )
        )

    suite = _resolve_json(
        raw.get("suite"),
        field="suite",
        resource_id=resource_id,
        repo_root=repo_root,
        issues=issues,
    )
    if suite is not None and not isinstance(suite.payload, Mapping):
        issues.append(
            CatalogIssue("error", "suite_not_object", "suite 根节点必须是对象", resource_id)
        )
    selection = _validate_track_selection(
        raw,
        suite=suite,
        resource_id=resource_id,
        issues=issues,
    )
    policy_document = _resolve_json(
        raw.get("policy"),
        field="policy",
        resource_id=resource_id,
        repo_root=repo_root,
        issues=issues,
    )
    policy = _validate_policy(
        policy_document,
        kind=kind,
        resource_id=resource_id,
        issues=issues,
    )

    baseline_raw = raw.get("baseline")
    if not isinstance(baseline_raw, Mapping):
        issues.append(CatalogIssue("error", "baseline_invalid", "baseline 必须是对象", resource_id))
        return resource_id, "baseline declaration invalid"
    status = baseline_raw.get("status")
    if status not in _BASELINE_STATUSES:
        issues.append(
            CatalogIssue(
                "error",
                "baseline_status_invalid",
                f"baseline.status 必须是 {sorted(_BASELINE_STATUSES)} 之一",
                resource_id,
            )
        )
    baseline = _resolve_json(
        baseline_raw.get("path"),
        field="baseline.path",
        resource_id=resource_id,
        repo_root=repo_root,
        issues=issues,
    )
    calibration_artifact = (
        _validate_retrieval_calibration(
            raw.get("calibration"),
            evaluation_suite=suite,
            baseline_ready=status == "ready",
            resource_id=resource_id,
            repo_root=repo_root,
            issues=issues,
        )
        if kind == "retrieval"
        else None
    )
    if status == "ready":
        _validate_ready_baseline(
            baseline,
            kind=kind,
            suite=suite,
            policy_document=policy_document,
            policy=policy,
            selection=selection,
            calibration_artifact=calibration_artifact,
            resource_id=resource_id,
            issues=issues,
        )
        return resource_id, "suite, policy and promoted baseline declared"
    if status == "rebuild_required":
        reason = baseline_raw.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            issues.append(
                CatalogIssue(
                    "error",
                    "baseline_rebuild_reason_missing",
                    "rebuild_required 必须提供非空 reason",
                    resource_id,
                )
            )
        issues.append(
            CatalogIssue(
                "warning",
                "baseline_rebuild_required",
                str(reason).strip() if isinstance(reason, str) else "baseline 需要重建",
                resource_id,
            )
        )
        return resource_id, "baseline rebuild required"
    return resource_id, "baseline status invalid"


def _validate_replay(
    raw: object,
    *,
    index: int,
    repo_root: Path,
    issues: list[CatalogIssue],
) -> tuple[str, str]:
    fallback = f"<replay-{index}>"
    if not isinstance(raw, Mapping):
        issues.append(CatalogIssue("error", "replay_not_object", "replay 必须是对象", fallback))
        return fallback, "invalid replay entry"
    resource_id, id_issue = _resource_id(raw.get("id"), fallback=fallback)
    if id_issue is not None:
        issues.append(id_issue)
    if raw.get("kind") != "event":
        issues.append(
            CatalogIssue(
                "error",
                "replay_kind_invalid",
                "replay kind 目前只支持 'event'",
                resource_id,
            )
        )
    if raw.get("mode") != "offline_validation_only":
        issues.append(
            CatalogIssue(
                "error",
                "replay_mode_invalid",
                "event replay 必须显式声明 offline_validation_only",
                resource_id,
            )
        )
    document = _resolve_json(
        raw.get("path"),
        field="replay.path",
        resource_id=resource_id,
        repo_root=repo_root,
        issues=issues,
    )
    if document is None:
        return resource_id, "replay bundle unavailable"
    if not isinstance(document.payload, Mapping):
        issues.append(
            CatalogIssue(
                "error",
                "replay_bundle_invalid",
                "replay bundle 必须是对象",
                resource_id,
            )
        )
        return resource_id, "replay bundle invalid"
    bundle = document.payload
    if (
        bundle.get("schema") != REPLAY_SCHEMA
        or bundle.get("schema_version") != REPLAY_SCHEMA_VERSION
    ):
        issues.append(
            CatalogIssue(
                "error",
                "replay_schema_invalid",
                f"replay 必须是 {REPLAY_SCHEMA} v{REPLAY_SCHEMA_VERSION}",
                resource_id,
            )
        )
    cases = bundle.get("cases")
    if not isinstance(cases, list) or not cases:
        issues.append(
            CatalogIssue(
                "error",
                "replay_cases_invalid",
                "replay.cases 必须是非空数组",
                resource_id,
            )
        )
    return resource_id, "offline event replay bundle declared"


def doctor_catalog(
    catalog_path: Path = DEFAULT_CATALOG,
    *,
    repo_root: Path = DEFAULT_REPO_ROOT,
) -> CatalogReport:
    """读取并验证 catalog；所有失败均结构化返回，不抛出给 CLI。"""

    issues: list[CatalogIssue] = []
    resources: list[tuple[ResourceType, str, str]] = []
    schema_version: str | None = None
    try:
        raw_bytes = catalog_path.resolve().read_bytes()
        payload = _decode_json(raw_bytes, label=str(catalog_path))
    except (OSError, ValueError) as error:
        issues.append(CatalogIssue("error", "catalog_unreadable", str(error)))
        return CatalogReport(
            str(catalog_path),
            str(repo_root.resolve()),
            schema_version,
            (),
            tuple(issues),
        )
    if not isinstance(payload, Mapping):
        issues.append(CatalogIssue("error", "catalog_not_object", "catalog 根节点必须是对象"))
        return CatalogReport(
            str(catalog_path),
            str(repo_root.resolve()),
            schema_version,
            (),
            tuple(issues),
        )

    raw_schema = payload.get("schema_version")
    schema_version = raw_schema if isinstance(raw_schema, str) else None
    if schema_version != CATALOG_SCHEMA_VERSION:
        issues.append(
            CatalogIssue(
                "error",
                "catalog_schema_invalid",
                f"schema_version 必须是 {CATALOG_SCHEMA_VERSION}",
            )
        )

    tracks = payload.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        issues.append(CatalogIssue("error", "tracks_invalid", "tracks 必须是非空数组"))
        tracks = []
    for index, raw_track in enumerate(tracks):
        resource_id, detail = _validate_track(
            raw_track,
            index=index,
            repo_root=repo_root,
            issues=issues,
        )
        resources.append(("track", resource_id, detail))

    replay_suites = payload.get("replay_suites")
    if not isinstance(replay_suites, list) or not replay_suites:
        issues.append(
            CatalogIssue("error", "replay_suites_invalid", "replay_suites 必须是非空数组")
        )
        replay_suites = []
    for index, raw_replay in enumerate(replay_suites):
        resource_id, detail = _validate_replay(
            raw_replay,
            index=index,
            repo_root=repo_root,
            issues=issues,
        )
        resources.append(("replay", resource_id, detail))

    seen: set[str] = set()
    for _, resource_id, _ in resources:
        if resource_id in seen:
            issues.append(
                CatalogIssue(
                    "error",
                    "resource_id_duplicate",
                    f"track 与 replay 的 id 必须全局唯一: {resource_id}",
                    resource_id,
                )
            )
        seen.add(resource_id)

    final_resources: list[CatalogResource] = []
    for resource_type, resource_id, detail in resources:
        owned = [issue for issue in issues if issue.resource_id == resource_id]
        if any(issue.severity == "error" for issue in owned):
            health: ResourceHealth = "invalid"
        elif any(issue.severity == "warning" for issue in owned):
            health = "warning"
        else:
            health = "ready"
        final_resources.append(CatalogResource(resource_type, resource_id, health, detail))

    return CatalogReport(
        str(catalog_path),
        str(repo_root.resolve()),
        schema_version,
        tuple(final_resources),
        tuple(issues),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.catalog",
        description="验证 WorkPilot 评测目录的 suite、policy、baseline 与 replay 契约",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor", help="只读检查评测目录")
    doctor.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    doctor.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command != "doctor":  # pragma: no cover - argparse 限制了命令集合
        raise AssertionError(f"未处理的命令: {args.command}")
    report = doctor_catalog(args.catalog)
    sys.stdout.write(report.to_json() if args.json else report.to_text())
    return 0 if report.healthy else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BASELINE_SCHEMA_VERSION",
    "CATALOG_SCHEMA_VERSION",
    "DEFAULT_CATALOG",
    "CatalogIssue",
    "CatalogReport",
    "CatalogResource",
    "doctor_catalog",
    "main",
]
