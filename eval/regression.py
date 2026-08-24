"""统一的、隐私安全的回归评测门禁。

现有 runner 的报告形状并不相同：检索/生成轨使用 ``items``，Cowork 又把
``observation`` 与 ``score`` 分层。这个模块先把它们投影成同一个逐样本契约，再做
严格配对比较。baseline 只保存数值、状态和哈希，不保存 prompt、答案、工具参数、
文件路径或引文原文，因此可以安全地提交进 Git。

退出码是稳定协议：0=通过，1=质量回退，2=前置条件不满足而拒绝判定。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from eval.report_metrics import METRICS, detect_kind

BASELINE_SCHEMA_VERSION = "workpilot-regression-baseline.v1"
REPORT_SCHEMA_VERSION = "workpilot-regression-report.v1"

_EVAL_ROOT = Path(__file__).resolve().parent
DEFAULT_POLICY_PATHS: dict[str, Path] = {
    "cowork": _EVAL_ROOT / "policies/cowork.json",
    "retrieval": _EVAL_ROOT / "policies/retrieval.json",
    "generation": _EVAL_ROOT / "policies/generation.json",
}

Direction = Literal["higher", "lower"]


class RegressionRefused(RuntimeError):
    """输入或 provenance 不可比；此时不能给出通过/失败结论。"""


class DuplicateJsonKeyError(ValueError):
    """JSON 对象含重复键，不能无歧义地签名或审计。"""


@dataclass(frozen=True)
class MetricPoint:
    """单样本对一个聚合指标的贡献；分母为 0 表示不适用。"""

    numerator: float
    denominator: float = 1.0

    @property
    def eligible(self) -> bool:
        return self.denominator > 0

    @property
    def value(self) -> float | None:
        return self.numerator / self.denominator if self.eligible else None

    def to_dict(self) -> dict[str, float]:
        return {"numerator": self.numerator, "denominator": self.denominator}


@dataclass(frozen=True)
class NormalizedCase:
    case_id: str
    segment: str
    status: str
    error: bool
    metrics: dict[str, MetricPoint]

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "segment": self.segment,
            "status": self.status,
            "error": self.error,
            "metrics": {name: point.to_dict() for name, point in sorted(self.metrics.items())},
        }


@dataclass(frozen=True)
class NormalizedReport:
    path: Path
    kind: str
    dataset: str
    dataset_version: str | None
    dataset_fingerprint: str | None
    config_fingerprint: str | None
    git_sha: str | None
    git_dirty: bool | None
    dataset_origin: str | None
    review_status: str | None
    reviewer: str | None
    reviewed_at: str | None
    split_counts: tuple[tuple[str, int], ...]
    calibration_fingerprint: str | None
    label: str | None
    baseline_policy_name: str | None
    baseline_policy_sha256: str | None
    cases: tuple[NormalizedCase, ...]

    @property
    def by_id(self) -> dict[str, NormalizedCase]:
        indexed: dict[str, NormalizedCase] = {}
        for case in self.cases:
            if case.case_id in indexed:
                raise RegressionRefused(f"报告含重复 case_id: {case.case_id} ({self.path})")
            indexed[case.case_id] = case
        return indexed


@dataclass(frozen=True)
class MetricRule:
    name: str
    direction: Direction
    required: bool = True
    max_absolute_regression: float = 0.0
    max_relative_regression: float = 0.0
    case_regression: bool = False
    pass_threshold: float | None = None
    min_candidate: float | None = None
    max_candidate: float | None = None


@dataclass(frozen=True)
class RegressionPolicy:
    schema_version: str
    name: str
    report_kind: str
    require_same_dataset: bool
    require_same_case_ids: bool
    require_same_dataset_fingerprint: bool
    require_same_config: bool
    fail_on_candidate_errors: bool
    metrics: tuple[MetricRule, ...]
    path: Path
    sha256: str


@dataclass(frozen=True)
class Violation:
    rule: str
    metric: str | None
    detail: str
    case_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegressionOutcome:
    kind: str
    dataset: str
    baseline_label: str | None
    candidate_label: str | None
    case_count: int
    policy_name: str
    config_drift_allowed: bool
    metrics: tuple[dict[str, Any], ...]
    violations: tuple[Violation, ...]

    @property
    def passed(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "passed": self.passed,
            "kind": self.kind,
            "dataset": self.dataset,
            "baseline_label": self.baseline_label,
            "candidate_label": self.candidate_label,
            "case_count": self.case_count,
            "policy": self.policy_name,
            "config_drift_allowed": self.config_drift_allowed,
            "metrics": list(self.metrics),
            "violations": [asdict(item) for item in self.violations],
        }


def canonical_json(value: object) -> str:
    """跨平台稳定的 JSON 表达，用于所有内容寻址。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"重复 JSON key: {key}")
        result[key] = value
    return result


def _number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RegressionRefused(f"{field} 必须是有限数字")
    result = float(value)
    if not math.isfinite(result):
        raise RegressionRefused(f"{field} 必须是有限数字")
    return result


def _point(value: object, *, field: str) -> MetricPoint:
    return MetricPoint(_number(value, field=field))


def _bool_point(value: object) -> MetricPoint:
    return MetricPoint(float(value is True))


def load_policy(path: Path) -> RegressionPolicy:
    resolved = path.resolve()
    try:
        raw_bytes = resolved.read_bytes()
        payload = json.loads(raw_bytes, object_pairs_hook=_unique_object)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        DuplicateJsonKeyError,
    ) as error:
        raise RegressionRefused(f"无法读取 policy {path}: {error}") from error
    if not isinstance(payload, dict):
        raise RegressionRefused("policy 根节点必须是对象")
    if payload.get("schema_version") != "workpilot-regression-policy.v1":
        raise RegressionRefused("policy schema_version 不受支持")
    raw_rules = payload.get("metrics")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RegressionRefused("policy.metrics 必须是非空数组")
    rules: list[MetricRule] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            raise RegressionRefused(f"policy.metrics[{index}] 必须是对象")
        name = str(raw.get("name") or "").strip()
        if not name or name in names:
            raise RegressionRefused(f"policy 指标名为空或重复: {name!r}")
        names.add(name)
        direction = raw.get("direction")
        if direction not in {"higher", "lower"}:
            raise RegressionRefused(f"指标 {name} 的 direction 必须是 higher/lower")
        absolute = _number(
            raw.get("max_absolute_regression", 0.0),
            field=f"policy.metrics[{index}].max_absolute_regression",
        )
        relative = _number(
            raw.get("max_relative_regression", 0.0),
            field=f"policy.metrics[{index}].max_relative_regression",
        )
        if absolute < 0 or relative < 0:
            raise RegressionRefused(f"指标 {name} 的回退容差不能为负")
        rules.append(
            MetricRule(
                name=name,
                direction=direction,
                required=bool(raw.get("required", True)),
                max_absolute_regression=absolute,
                max_relative_regression=relative,
                case_regression=bool(raw.get("case_regression", False)),
                pass_threshold=(
                    None
                    if raw.get("pass_threshold") is None
                    else _number(raw["pass_threshold"], field=f"{name}.pass_threshold")
                ),
                min_candidate=(
                    None
                    if raw.get("min_candidate") is None
                    else _number(raw["min_candidate"], field=f"{name}.min_candidate")
                ),
                max_candidate=(
                    None
                    if raw.get("max_candidate") is None
                    else _number(raw["max_candidate"], field=f"{name}.max_candidate")
                ),
            )
        )
    return RegressionPolicy(
        schema_version=str(payload["schema_version"]),
        name=str(payload.get("name") or resolved.stem),
        report_kind=str(payload.get("report_kind") or ""),
        require_same_dataset=bool(payload.get("require_same_dataset", True)),
        require_same_case_ids=bool(payload.get("require_same_case_ids", True)),
        require_same_dataset_fingerprint=bool(
            payload.get("require_same_dataset_fingerprint", True)
        ),
        require_same_config=bool(payload.get("require_same_config", True)),
        fail_on_candidate_errors=bool(payload.get("fail_on_candidate_errors", True)),
        metrics=tuple(rules),
        path=resolved,
        sha256=content_sha256(payload),
    )


def _resolve_report_path(path: Path) -> Path:
    resolved = path / "report.json" if path.is_dir() else path
    if not resolved.is_file():
        raise RegressionRefused(f"报告不存在: {resolved}")
    return resolved.resolve()


def load_normalized_report(path: Path) -> NormalizedReport:
    resolved = _resolve_report_path(path)
    try:
        payload = json.loads(resolved.read_bytes(), object_pairs_hook=_unique_object)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        DuplicateJsonKeyError,
    ) as error:
        raise RegressionRefused(f"无法读取报告 {resolved}: {error}") from error
    if not isinstance(payload, dict):
        raise RegressionRefused(f"报告根节点必须是对象: {resolved}")
    if payload.get("schema_version") == BASELINE_SCHEMA_VERSION:
        return _load_baseline_payload(payload, resolved)
    kind = _detect_kind(payload, resolved)
    if kind == "cowork":
        return _normalize_cowork(payload, resolved)
    return _normalize_legacy(payload, resolved, kind)


def _detect_kind(payload: dict[str, Any], path: Path) -> str:
    explicit = payload.get("kind")
    if explicit in {"cowork", "retrieval", "generation"}:
        return str(explicit)
    if str(payload.get("schema_version") or "").startswith("cowork-eval-report"):
        return "cowork"
    items = payload.get("items")
    if isinstance(items, dict) and "task_success_rate" in (payload.get("metrics") or {}):
        return "cowork"
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict) and "score" in first and "observation" in first:
            return "cowork"
        if isinstance(first, dict):
            return detect_kind(first, path)
    raise RegressionRefused(f"无法识别报告类型: {path}")


def _normalize_cowork(payload: dict[str, Any], path: Path) -> NormalizedReport:
    raw_items = payload.get("items")
    cases: list[NormalizedCase] = []
    split_counts: tuple[tuple[str, int], ...] = ()
    # 旧的 cowork.json 只存了 item_id -> bool。保留读取能力，方便给出明确的 suite
    # 不兼容结论；新 baseline 一律由 snapshot 子命令生成完整逐指标投影。
    if isinstance(raw_items, dict):
        for case_id, passed in sorted(raw_items.items()):
            cases.append(
                NormalizedCase(
                    case_id=str(case_id),
                    segment="unknown",
                    status="done" if passed is True else "failed",
                    error=False,
                    metrics={"task_success": _bool_point(passed)},
                )
            )
    elif isinstance(raw_items, list) and raw_items:
        split_counts = _split_counts(raw_items)
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                raise RegressionRefused(f"Cowork items[{index}] 必须是对象")
            case_id = str(item.get("item_id") or "").strip()
            if not case_id:
                raise RegressionRefused(f"Cowork items[{index}] 缺少 item_id")
            score = item.get("score")
            observation = item.get("observation")
            if not isinstance(score, dict) or not isinstance(observation, dict):
                raise RegressionRefused(f"Cowork item {case_id} 缺少 score/observation")
            metrics = _cowork_metrics(score, observation, case_id=case_id)
            status = str(observation.get("status") or "unknown")
            error = status == "runner_error" or bool(observation.get("error"))
            cases.append(
                NormalizedCase(
                    case_id=case_id,
                    segment=str(item.get("category") or "unknown"),
                    status=status,
                    error=error,
                    metrics=metrics,
                )
            )
    else:
        raise RegressionRefused(f"Cowork 报告缺少非空 items: {path}")

    manifest_value = payload.get("manifest")
    manifest: dict[str, Any] = dict(manifest_value) if isinstance(manifest_value, dict) else {}
    dataset = str(payload.get("dataset") or payload.get("suite") or "")
    if not dataset:
        raise RegressionRefused(f"Cowork 报告缺少 suite/dataset: {path}")
    dataset_fingerprint = _optional_string(
        manifest.get("suite_sha256") or payload.get("suite_sha256")
    )
    config_fingerprint = _optional_string(payload.get("config_hash") or manifest.get("config_hash"))
    if config_fingerprint is None and manifest:
        config_fingerprint = content_sha256(_cowork_controlled_config(manifest))
    reproducibility_value = manifest.get("reproducibility")
    reproducibility: dict[str, Any] = (
        dict(reproducibility_value) if isinstance(reproducibility_value, dict) else {}
    )
    return NormalizedReport(
        path=path,
        kind="cowork",
        dataset=dataset,
        dataset_version=_optional_string(payload.get("suite_version")),
        dataset_fingerprint=dataset_fingerprint,
        config_fingerprint=config_fingerprint,
        git_sha=_optional_string(payload.get("git_sha") or reproducibility.get("git_sha")),
        git_dirty=_optional_bool(reproducibility.get("git_dirty")),
        dataset_origin=_optional_string(manifest.get("suite_origin")),
        review_status=_optional_string(manifest.get("suite_review_status")),
        reviewer=_optional_string(manifest.get("suite_reviewer")),
        reviewed_at=_optional_string(manifest.get("suite_reviewed_at")),
        split_counts=split_counts,
        calibration_fingerprint=None,
        label=_optional_string(payload.get("label") or manifest.get("label")),
        baseline_policy_name=None,
        baseline_policy_sha256=None,
        cases=tuple(cases),
    )


def _cowork_controlled_config(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "suite_sha256": manifest.get("suite_sha256"),
        "item_ids": manifest.get("item_ids"),
        "model": manifest.get("model"),
        "budgets": manifest.get("budgets"),
        "fixture_policy": manifest.get("fixture_policy"),
        "mode": manifest.get("mode", "generation"),
    }


def _cowork_metrics(
    score: Mapping[str, Any], observation: Mapping[str, Any], *, case_id: str
) -> dict[str, MetricPoint]:
    selection = score.get("tool_selection")
    selection = selection if isinstance(selection, dict) else {}
    metrics = {
        "task_success": _bool_point(score.get("task_success")),
        "status_match": _bool_point(score.get("status_match")),
        "assertions_pass": _bool_point(score.get("assertions_pass")),
        "guardrail_pass": _bool_point(score.get("guardrail_pass")),
        "tool_selection_accuracy": _bool_point(selection.get("passed")),
        "within_tool_budget": _bool_point(score.get("within_tool_budget")),
        "step_efficiency": _point(
            score.get("step_efficiency"), field=f"{case_id}.score.step_efficiency"
        ),
        "used_tokens": _point(
            observation.get("used_tokens", 0),
            field=f"{case_id}.observation.used_tokens",
        ),
        "used_calls": _point(
            observation.get("used_calls", 0), field=f"{case_id}.observation.used_calls"
        ),
    }
    trace = observation.get("tool_trace")
    if isinstance(trace, list) and trace:
        failed = sum(isinstance(call, dict) and call.get("status") == "failed" for call in trace)
        metrics["tool_error_rate"] = MetricPoint(float(failed), float(len(trace)))
    reading = score.get("reading")
    if isinstance(reading, dict):
        for name in ("read_before_claim", "quote_verifiability", "locator_accuracy"):
            tally = reading.get(name)
            if not isinstance(tally, dict):
                continue
            total = _number(tally.get("total", 0), field=f"{case_id}.reading.{name}.total")
            passed = _number(tally.get("passed", 0), field=f"{case_id}.reading.{name}.passed")
            if total < 0 or passed < 0 or passed > total:
                raise RegressionRefused(f"{case_id}.reading.{name} 分子分母无效")
            metrics[name] = MetricPoint(passed, total)
    return metrics


def _normalize_legacy(payload: dict[str, Any], path: Path, kind: str) -> NormalizedReport:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise RegressionRefused(f"{kind} 报告缺少非空 items: {path}")
    config_value = payload.get("config")
    config: dict[str, Any] = dict(config_value) if isinstance(config_value, dict) else {}
    cases: list[NormalizedCase] = []
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            raise RegressionRefused(f"{kind} items[{index}] 必须是对象")
        item: dict[str, Any] = dict(raw_item)
        case_id = str(item.get("item_id") or "").strip()
        if not case_id:
            raise RegressionRefused(f"{kind} items[{index}] 缺少 item_id")
        metrics: dict[str, MetricPoint] = {}
        for spec in METRICS[kind]:
            point = spec.extract(item, config)
            if point.eligible:
                metrics[spec.name] = MetricPoint(point.numerator, point.denominator)
        cases.append(
            NormalizedCase(
                case_id=case_id,
                segment=str(item.get("category") or "unknown"),
                status="failed" if item.get("error") is not None else "done",
                error=item.get("error") is not None,
                metrics=metrics,
            )
        )
    suite_value = payload.get("suite")
    suite: dict[str, Any] = dict(suite_value) if isinstance(suite_value, dict) else {}
    reproducibility_value = payload.get("reproducibility")
    reproducibility = (
        dict(reproducibility_value) if isinstance(reproducibility_value, dict) else {}
    )
    calibration_value = config.get("refusal_calibration")
    calibration = dict(calibration_value) if isinstance(calibration_value, dict) else {}
    fingerprint = _optional_string(
        payload.get("dataset_fingerprint")
        or config.get("dataset_fingerprint")
        or suite.get("sha256")
    )
    return NormalizedReport(
        path=path,
        kind=kind,
        dataset=str(payload.get("dataset") or config.get("dataset") or "unknown"),
        dataset_version=_optional_string(suite.get("version")),
        dataset_fingerprint=fingerprint,
        config_fingerprint=_optional_string(payload.get("config_hash")) or content_sha256(config),
        git_sha=_optional_string(payload.get("git_sha")),
        git_dirty=_optional_bool(reproducibility.get("git_dirty")),
        dataset_origin=_optional_string(suite.get("origin") or config.get("origin")),
        review_status=_optional_string(suite.get("review_status")),
        reviewer=_optional_string(suite.get("reviewer")),
        reviewed_at=_optional_string(suite.get("reviewed_at")),
        split_counts=_split_counts(raw_items),
        calibration_fingerprint=_optional_string(calibration.get("sha256")),
        label=_optional_string(payload.get("label")),
        baseline_policy_name=None,
        baseline_policy_sha256=None,
        cases=tuple(cases),
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _split_counts(items: Sequence[object]) -> tuple[tuple[str, int], ...]:
    counts = Counter(
        str(item.get("split"))
        for item in items
        if isinstance(item, Mapping) and isinstance(item.get("split"), str)
    )
    return tuple(sorted(counts.items()))


def _load_baseline_payload(payload: dict[str, Any], path: Path) -> NormalizedReport:
    integrity = payload.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("algorithm") != "sha256":
        raise RegressionRefused(f"baseline 缺少 sha256 完整性信息: {path}")
    unsigned = {key: value for key, value in payload.items() if key != "integrity"}
    expected = str(integrity.get("value") or "")
    actual = content_sha256(unsigned)
    if not expected or expected != actual:
        raise RegressionRefused(f"baseline 完整性校验失败: {path}")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise RegressionRefused(f"baseline 缺少 cases: {path}")
    policy = payload.get("policy")
    if not isinstance(policy, dict):
        raise RegressionRefused(f"baseline 缺少 policy 绑定: {path}")
    policy_name = _optional_string(policy.get("name"))
    policy_sha256 = _optional_string(policy.get("sha256"))
    if (
        policy_name is None
        or policy_sha256 is None
        or len(policy_sha256) != 64
        or any(character not in "0123456789abcdef" for character in policy_sha256)
    ):
        raise RegressionRefused(f"baseline policy 绑定不完整: {path}")
    review_value = payload.get("review")
    review = dict(review_value) if isinstance(review_value, dict) else {}
    selection_value = payload.get("selection")
    selection = dict(selection_value) if isinstance(selection_value, dict) else {}
    raw_split_counts = selection.get("split_counts")
    split_counts = (
        tuple(
            sorted(
                (str(key), int(value))
                for key, value in raw_split_counts.items()
                if isinstance(key, str)
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0
            )
        )
        if isinstance(raw_split_counts, dict)
        else ()
    )
    cases: list[NormalizedCase] = []
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, dict) or not isinstance(raw.get("metrics"), dict):
            raise RegressionRefused(f"baseline cases[{index}] 格式无效")
        metrics: dict[str, MetricPoint] = {}
        for name, point in raw["metrics"].items():
            if not isinstance(point, dict):
                raise RegressionRefused(f"baseline {raw.get('case_id')}.{name} 格式无效")
            metrics[str(name)] = MetricPoint(
                _number(point.get("numerator"), field=f"baseline.{name}.numerator"),
                _number(point.get("denominator"), field=f"baseline.{name}.denominator"),
            )
        cases.append(
            NormalizedCase(
                case_id=str(raw.get("case_id") or ""),
                segment=str(raw.get("segment") or "unknown"),
                status=str(raw.get("status") or "unknown"),
                error=bool(raw.get("error", False)),
                metrics=metrics,
            )
        )
    return NormalizedReport(
        path=path,
        kind=str(payload.get("kind") or ""),
        dataset=str(payload.get("dataset") or ""),
        dataset_version=_optional_string(payload.get("dataset_version")),
        dataset_fingerprint=_optional_string(payload.get("dataset_fingerprint")),
        config_fingerprint=_optional_string(payload.get("config_fingerprint")),
        git_sha=_optional_string(payload.get("git_sha")),
        git_dirty=_optional_bool(payload.get("git_dirty")),
        dataset_origin=_optional_string(review.get("origin")),
        review_status=_optional_string(review.get("status")),
        reviewer=_optional_string(review.get("reviewer")),
        reviewed_at=_optional_string(review.get("reviewed_at")),
        split_counts=split_counts,
        calibration_fingerprint=_optional_string(payload.get("calibration_fingerprint")),
        label=_optional_string(payload.get("label")),
        baseline_policy_name=policy_name,
        baseline_policy_sha256=policy_sha256,
        cases=tuple(cases),
    )


def build_baseline(report: NormalizedReport, policy: RegressionPolicy) -> dict[str, Any]:
    _validate_policy_kind(report, policy)
    _validate_snapshot_provenance(report, policy)
    errors = [case.case_id for case in report.cases if case.error]
    if errors:
        raise RegressionRefused(
            f"含失败样本的报告不能晋升 baseline: {errors[:5]}（共 {len(errors)} 条）"
        )
    _validate_required_metrics(report, policy)
    payload: dict[str, Any] = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "kind": report.kind,
        "dataset": report.dataset,
        "dataset_version": report.dataset_version,
        "dataset_fingerprint": report.dataset_fingerprint,
        "config_fingerprint": report.config_fingerprint,
        "git_sha": report.git_sha,
        "git_dirty": report.git_dirty,
        "review": {
            "origin": report.dataset_origin,
            "status": report.review_status,
            "reviewer": report.reviewer,
            "reviewed_at": report.reviewed_at,
        },
        "selection": {"split_counts": dict(report.split_counts)},
        "calibration_fingerprint": report.calibration_fingerprint,
        "label": report.label,
        "policy": {"name": policy.name, "sha256": policy.sha256},
        "source_report_sha256": hashlib.sha256(report.path.read_bytes()).hexdigest(),
        "cases": [
            case.to_snapshot() for case in sorted(report.cases, key=lambda item: item.case_id)
        ],
    }
    payload["integrity"] = {"algorithm": "sha256", "value": content_sha256(payload)}
    return payload


def _validate_snapshot_provenance(report: NormalizedReport, policy: RegressionPolicy) -> None:
    if not report.cases:
        raise RegressionRefused("空报告不能晋升 baseline")
    _ = report.by_id  # 触发重复 case_id 校验。
    if not report.dataset or report.dataset == "unknown":
        raise RegressionRefused("缺少 dataset 的报告不能晋升 baseline")
    if policy.require_same_dataset_fingerprint and not _is_sha256(report.dataset_fingerprint):
        raise RegressionRefused("缺少合法 dataset/suite fingerprint 的报告不能晋升 baseline")
    if policy.require_same_config and not _is_sha256(report.config_fingerprint):
        raise RegressionRefused("缺少合法 config fingerprint 的报告不能晋升 baseline")
    if not isinstance(report.git_sha, str) or not re.fullmatch(
        r"(?:[0-9a-f]{40}|[0-9a-f]{64})", report.git_sha
    ):
        raise RegressionRefused("缺少合法 Git SHA 的报告不能晋升 baseline")
    if report.git_dirty is not False:
        raise RegressionRefused("Git dirty 或状态缺失的报告不能晋升 baseline")
    if report.review_status != "approved" or not report.reviewer or not report.reviewed_at:
        raise RegressionRefused(
            "题库未 approved 或缺少 reviewer/reviewed_at，不能晋升 baseline"
        )
    try:
        reviewed_at = datetime.fromisoformat(report.reviewed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise RegressionRefused("reviewed_at 不是合法 ISO-8601 时间，不能晋升 baseline") from error
    if reviewed_at.tzinfo is None:
        raise RegressionRefused("reviewed_at 必须包含时区，不能晋升 baseline")
    if not report.dataset_origin:
        raise RegressionRefused("报告缺少 suite origin，不能晋升 baseline")
    if not report.split_counts:
        raise RegressionRefused("报告缺少 split 选择信息，不能晋升 baseline")
    if report.kind == "retrieval" and not _is_sha256(report.calibration_fingerprint):
        raise RegressionRefused("检索报告缺少独立拒答校准 fingerprint，不能晋升 baseline")
    if not report.label:
        raise RegressionRefused("缺少 run label 的报告不能晋升 baseline")


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_policy_kind(report: NormalizedReport, policy: RegressionPolicy) -> None:
    if policy.report_kind != report.kind:
        raise RegressionRefused(
            f"policy/report 类型不一致: policy={policy.report_kind}, report={report.kind}"
        )


def _validate_required_metrics(report: NormalizedReport, policy: RegressionPolicy) -> None:
    for rule in policy.metrics:
        eligible = [
            case.case_id
            for case in report.cases
            if (point := case.metrics.get(rule.name)) is not None and point.eligible
        ]
        if rule.required and not eligible:
            raise RegressionRefused(f"报告没有指标 {rule.name} 的任何有效样本")


def evaluate_regression(
    baseline: NormalizedReport,
    candidate: NormalizedReport,
    policy: RegressionPolicy,
    *,
    allow_config_drift: bool = False,
) -> RegressionOutcome:
    _validate_baseline_policy(baseline, policy)
    _validate_policy_kind(baseline, policy)
    _validate_policy_kind(candidate, policy)
    _validate_compatibility(
        baseline,
        candidate,
        policy,
        allow_config_drift=allow_config_drift,
    )
    baseline_by_id = baseline.by_id
    candidate_by_id = candidate.by_id
    ids = tuple(sorted(set(baseline_by_id) & set(candidate_by_id)))
    violations: list[Violation] = []
    if policy.fail_on_candidate_errors:
        errors = tuple(case_id for case_id in ids if candidate_by_id[case_id].error)
        if errors:
            violations.append(
                Violation(
                    rule="candidate_error",
                    metric=None,
                    detail=f"候选报告含 {len(errors)} 条基础设施/runner 失败",
                    case_ids=errors,
                )
            )
    checks: list[dict[str, Any]] = []
    for rule in policy.metrics:
        check, metric_violations = _evaluate_metric(
            ids,
            baseline_by_id,
            candidate_by_id,
            rule,
        )
        checks.append(check)
        violations.extend(metric_violations)
    return RegressionOutcome(
        kind=candidate.kind,
        dataset=candidate.dataset,
        baseline_label=baseline.label,
        candidate_label=candidate.label,
        case_count=len(ids),
        policy_name=policy.name,
        config_drift_allowed=allow_config_drift,
        metrics=tuple(checks),
        violations=tuple(violations),
    )


def _validate_baseline_policy(baseline: NormalizedReport, policy: RegressionPolicy) -> None:
    """Baseline 与门禁规则共同定义批准状态，二者不能各自静默漂移。"""

    if baseline.baseline_policy_name is None or baseline.baseline_policy_sha256 is None:
        raise RegressionRefused(
            "baseline 必须是由 eval.regression snapshot 生成且绑定 policy 的快照"
        )
    if (
        baseline.baseline_policy_name != policy.name
        or baseline.baseline_policy_sha256 != policy.sha256
    ):
        raise RegressionRefused(
            "baseline 绑定的 policy 与当前 policy 不一致；规则变更后必须重新审核并晋升 baseline"
        )


def _validate_compatibility(
    baseline: NormalizedReport,
    candidate: NormalizedReport,
    policy: RegressionPolicy,
    *,
    allow_config_drift: bool,
) -> None:
    if baseline.git_dirty is not False or candidate.git_dirty is not False:
        raise RegressionRefused("baseline/candidate 必须都来自 Git clean 工作树")
    if (
        baseline.review_status != "approved"
        or candidate.review_status != "approved"
        or not baseline.reviewer
        or not candidate.reviewer
        or not baseline.reviewed_at
        or not candidate.reviewed_at
    ):
        raise RegressionRefused("baseline/candidate 都必须固定已批准题库的复核 provenance")
    if any(
        getattr(baseline, field) != getattr(candidate, field)
        for field in ("dataset_origin", "review_status", "reviewer", "reviewed_at")
    ):
        raise RegressionRefused("baseline/candidate 的题库复核 provenance 不一致")
    if baseline.kind != candidate.kind:
        raise RegressionRefused(
            f"报告类型不一致: baseline={baseline.kind}, candidate={candidate.kind}"
        )
    if policy.require_same_dataset and baseline.dataset != candidate.dataset:
        raise RegressionRefused(
            f"数据集不一致: baseline={baseline.dataset}, candidate={candidate.dataset}"
        )
    if policy.require_same_dataset_fingerprint:
        if not baseline.dataset_fingerprint or not candidate.dataset_fingerprint:
            raise RegressionRefused("缺少 dataset/suite fingerprint，无法确认标注未漂移")
        if baseline.dataset_fingerprint != candidate.dataset_fingerprint:
            raise RegressionRefused("dataset/suite fingerprint 不一致，标注或样本已漂移")
    baseline_ids = set(baseline.by_id)
    candidate_ids = set(candidate.by_id)
    if policy.require_same_case_ids and baseline_ids != candidate_ids:
        only_baseline = sorted(baseline_ids - candidate_ids)
        only_candidate = sorted(candidate_ids - baseline_ids)
        raise RegressionRefused(
            "case_id 集合不一致，不能做配对门禁: "
            f"仅 baseline={only_baseline[:5]}, 仅 candidate={only_candidate[:5]}"
        )
    for case_id in baseline_ids & candidate_ids:
        if baseline.by_id[case_id].segment != candidate.by_id[case_id].segment:
            raise RegressionRefused(f"case {case_id} 的 segment 漂移")
    if policy.require_same_config and not allow_config_drift:
        if not baseline.config_fingerprint or not candidate.config_fingerprint:
            raise RegressionRefused("缺少 config fingerprint，无法确认两次运行可比")
        if baseline.config_fingerprint != candidate.config_fingerprint:
            raise RegressionRefused(
                "受控配置 fingerprint 不一致；确认是有意实验后使用 --allow-config-drift"
            )
    if baseline.split_counts != candidate.split_counts:
        raise RegressionRefused(
            f"split 选择不一致: baseline={dict(baseline.split_counts)}, "
            f"candidate={dict(candidate.split_counts)}"
        )
    if baseline.kind == "retrieval":
        if not baseline.calibration_fingerprint or not candidate.calibration_fingerprint:
            raise RegressionRefused("retrieval baseline/candidate 缺少独立校准 fingerprint")
        if baseline.calibration_fingerprint != candidate.calibration_fingerprint:
            raise RegressionRefused("retrieval baseline/candidate 使用了不同的独立拒答校准")


def _evaluate_metric(
    ids: Sequence[str],
    baseline: Mapping[str, NormalizedCase],
    candidate: Mapping[str, NormalizedCase],
    rule: MetricRule,
) -> tuple[dict[str, Any], list[Violation]]:
    paired: list[tuple[str, MetricPoint, MetricPoint]] = []
    eligibility_drift: list[str] = []
    for case_id in ids:
        left = baseline[case_id].metrics.get(rule.name)
        right = candidate[case_id].metrics.get(rule.name)
        left_ok = left is not None and left.eligible
        right_ok = right is not None and right.eligible
        if left_ok != right_ok:
            eligibility_drift.append(case_id)
            continue
        if left_ok and right_ok:
            assert left is not None and right is not None
            paired.append((case_id, left, right))
    if eligibility_drift:
        raise RegressionRefused(f"指标 {rule.name} 的适用样本发生漂移: {eligibility_drift[:5]}")
    if not paired:
        if rule.required:
            raise RegressionRefused(f"指标 {rule.name} 没有任何可配对样本")
        return {
            "metric": rule.name,
            "direction": rule.direction,
            "status": "not_applicable",
            "sample_count": 0,
        }, []
    baseline_value = _aggregate([left for _, left, _ in paired])
    candidate_value = _aggregate([right for _, _, right in paired])
    assert baseline_value is not None and candidate_value is not None
    delta = candidate_value - baseline_value
    gain = delta if rule.direction == "higher" else -delta
    allowed_regression = max(
        rule.max_absolute_regression,
        abs(baseline_value) * rule.max_relative_regression,
    )
    improved: list[str] = []
    regressed: list[str] = []
    for case_id, left, right in paired:
        assert left.value is not None and right.value is not None
        case_gain = (
            right.value - left.value if rule.direction == "higher" else left.value - right.value
        )
        if case_gain > 0:
            improved.append(case_id)
        elif case_gain < 0:
            regressed.append(case_id)
    check = {
        "metric": rule.name,
        "direction": rule.direction,
        "status": "passed",
        "baseline": baseline_value,
        "candidate": candidate_value,
        "delta": delta,
        "gain": gain,
        "allowed_regression": allowed_regression,
        "sample_count": len(paired),
        "improved_samples": len(improved),
        "regressed_samples": len(regressed),
        "regressed_case_ids": regressed,
    }
    violations: list[Violation] = []
    if gain < -allowed_regression - 1e-12:
        check["status"] = "failed"
        violations.append(
            Violation(
                rule="aggregate_regression",
                metric=rule.name,
                detail=(
                    f"{baseline_value:.6g} → {candidate_value:.6g}，"
                    f"回退 {-gain:.6g}，允许 {allowed_regression:.6g}"
                ),
                case_ids=tuple(regressed),
            )
        )
    threshold = rule.pass_threshold
    if rule.case_regression:
        pass_to_fail: list[str] = []
        for case_id, left, right in paired:
            left_value = left.value
            right_value = right.value
            assert left_value is not None and right_value is not None
            if threshold is not None:
                lost = left_value >= threshold > right_value
            else:
                lost = (
                    right_value < left_value
                    if rule.direction == "higher"
                    else right_value > left_value
                )
            if lost:
                pass_to_fail.append(case_id)
        if pass_to_fail:
            check["status"] = "failed"
            violations.append(
                Violation(
                    rule="case_regression",
                    metric=rule.name,
                    detail=f"{len(pass_to_fail)} 条已通过样本发生回退",
                    case_ids=tuple(pass_to_fail),
                )
            )
    if rule.min_candidate is not None and candidate_value < rule.min_candidate:
        check["status"] = "failed"
        violations.append(
            Violation(
                rule="minimum",
                metric=rule.name,
                detail=f"候选 {candidate_value:.6g} 低于下限 {rule.min_candidate:.6g}",
            )
        )
    if rule.max_candidate is not None and candidate_value > rule.max_candidate:
        check["status"] = "failed"
        violations.append(
            Violation(
                rule="maximum",
                metric=rule.name,
                detail=f"候选 {candidate_value:.6g} 高于上限 {rule.max_candidate:.6g}",
            )
        )
    return check, violations


def _aggregate(points: Sequence[MetricPoint]) -> float | None:
    numerator = sum(point.numerator for point in points if point.eligible)
    denominator = sum(point.denominator for point in points if point.eligible)
    return numerator / denominator if denominator else None


def render_markdown(outcome: RegressionOutcome) -> str:
    verdict = "✅ 通过" if outcome.passed else "❌ 阻断"
    lines = [
        f"# 回归门禁：{verdict}",
        "",
        f"- 评测轨：`{outcome.kind}`",
        f"- 数据集：`{outcome.dataset}`（{outcome.case_count} 条）",
        f"- Policy：`{outcome.policy_name}`",
        f"- Baseline / Candidate：`{outcome.baseline_label}` → `{outcome.candidate_label}`",
        "",
    ]
    if outcome.violations:
        lines.extend(["## 阻断项", "", "| 规则 | 指标 | 详情 | case |", "|---|---|---|---|"])
        for item in outcome.violations:
            shown = ", ".join(item.case_ids[:8])
            if len(item.case_ids) > 8:
                shown += f" …(+{len(item.case_ids) - 8})"
            lines.append(
                f"| `{item.rule}` | `{item.metric or '-'}` | {item.detail} | {shown or '-'} |"
            )
        lines.append("")
    lines.extend(
        [
            "## 指标",
            "",
            "| 指标 | baseline | candidate | Δ | 允许回退 | 配对样本 | 判定 |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for metric in outcome.metrics:
        if metric["status"] == "not_applicable":
            lines.append(f"| `{metric['metric']}` | n/a | n/a | n/a | n/a | 0 | 跳过 |")
            continue
        lines.append(
            f"| `{metric['metric']}` | {metric['baseline']:.6g} | "
            f"{metric['candidate']:.6g} | {metric['delta']:+.6g} | "
            f"{metric['allowed_regression']:.6g} | {metric['sample_count']} | "
            f"{'通过' if metric['status'] == 'passed' else '阻断'} |"
        )
    lines.append("")
    return "\n".join(lines)


def _default_policy(kind: str) -> Path:
    try:
        return DEFAULT_POLICY_PATHS[kind]
    except KeyError as error:
        raise RegressionRefused(f"没有为 {kind} 定义默认 policy") from error


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WorkPilot 统一回归评测门禁")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="把可信报告晋升为隐私安全 baseline")
    snapshot.add_argument("report", type=Path)
    snapshot.add_argument("--policy", type=Path)
    snapshot.add_argument("--output", type=Path, required=True)

    check = subparsers.add_parser("check", help="将候选报告与固定 baseline 配对比较")
    check.add_argument("candidate", type=Path)
    check.add_argument("--baseline", type=Path, required=True)
    check.add_argument("--policy", type=Path)
    check.add_argument("--allow-config-drift", action="store_true")
    check.add_argument("--output-dir", type=Path)
    return parser.parse_args(argv)


def _run_snapshot(args: argparse.Namespace) -> int:
    report = load_normalized_report(args.report)
    policy = load_policy(args.policy or _default_policy(report.kind))
    snapshot = build_baseline(report, policy)
    output: Path = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x", encoding="utf-8") as stream:
            json.dump(snapshot, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
    except FileExistsError as error:
        raise RegressionRefused(f"输出已存在，拒绝覆盖: {output}") from error
    print(f"baseline 已写入 {output}（{len(snapshot['cases'])} 条，仅含指标与哈希）")
    return 0


def _run_check(args: argparse.Namespace) -> int:
    baseline = load_normalized_report(args.baseline)
    candidate = load_normalized_report(args.candidate)
    policy = load_policy(args.policy or _default_policy(candidate.kind))
    outcome = evaluate_regression(
        baseline,
        candidate,
        policy,
        allow_config_drift=bool(args.allow_config_drift),
    )
    markdown = render_markdown(outcome)
    print(markdown)
    if args.output_dir is not None:
        output_dir: Path = args.output_dir
        output_dir.mkdir(parents=True, exist_ok=False)
        (output_dir / "report.json").write_text(
            json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (output_dir / "report.md").write_text(markdown, encoding="utf-8")
    return 0 if outcome.passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return _run_snapshot(args) if args.command == "snapshot" else _run_check(args)
    except RegressionRefused as error:
        print(f"回归门禁拒绝判定：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
