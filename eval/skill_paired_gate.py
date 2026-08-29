"""Skill enabled/disabled 的隐私安全严格配对门禁。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.cowork_task_suite import load_suite, suite_review
from eval.stats import MetricSamples, RatioPoint, paired_bootstrap

SCHEMA_VERSION = "workpilot-skill-paired-gate.v1"


class SkillPairedGateError(RuntimeError):
    """两臂 provenance 不可比或 suite 缺少成对协议。"""


@dataclass(frozen=True)
class ArmPoint:
    success: bool
    guardrail: bool
    tokens: int
    calls: int
    tool_errors: int
    tool_total: int
    loaded_skills: tuple[str, ...]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SkillPairedGateError(f"无法读取 {path}: {error}") from error
    if not isinstance(value, dict):
        raise SkillPairedGateError(f"{path}: JSON 根节点必须是对象")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pairing_contract(suite: dict[str, Any]) -> dict[str, Any]:
    raw = suite.get("skill_pairing")
    if not isinstance(raw, dict):
        raise SkillPairedGateError("suite 缺少 skill_pairing contract")
    expected = raw.get("expected_skill")
    trigger = raw.get("trigger_item_ids")
    anti = raw.get("anti_trigger_item_ids")
    gate = raw.get("gate")
    if not isinstance(expected, str) or not expected.strip():
        raise SkillPairedGateError("skill_pairing.expected_skill 非法")
    if not isinstance(trigger, list) or not trigger or not all(isinstance(x, str) for x in trigger):
        raise SkillPairedGateError("skill_pairing.trigger_item_ids 非法")
    if not isinstance(anti, list) or not anti or not all(isinstance(x, str) for x in anti):
        raise SkillPairedGateError("skill_pairing.anti_trigger_item_ids 非法")
    if set(trigger) & set(anti) or set(trigger) | set(anti) != {
        str(item["id"]) for item in suite["items"]
    }:
        raise SkillPairedGateError("trigger/anti-trigger 必须无重叠且完整覆盖 suite")
    if not isinstance(gate, dict):
        raise SkillPairedGateError("skill_pairing.gate 非法")
    required_numbers = {
        "minimum_trigger_activation_rate",
        "minimum_trigger_success_gain",
        "maximum_anti_trigger_activation_rate",
        "maximum_token_regression",
        "maximum_call_regression",
        "maximum_tool_error_rate_increase",
    }
    if any(
        isinstance(gate.get(name), bool) or not isinstance(gate.get(name), int | float)
        for name in required_numbers
    ):
        raise SkillPairedGateError("skill_pairing.gate 缺少有限数值阈值")
    return raw


def _sanitized_config(report: dict[str, Any]) -> dict[str, Any]:
    manifest = report.get("manifest")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("config"), dict):
        raise SkillPairedGateError("Cowork report 缺少 manifest.config")
    config = copy.deepcopy(manifest["config"])
    runtime = config.get("runtime")
    if not isinstance(runtime, dict):
        raise SkillPairedGateError("Cowork report 缺少 runtime config")
    runtime.pop("skills_mode", None)
    return config


def _validate_reports(
    suite_path: Path,
    suite: dict[str, Any],
    off: dict[str, Any],
    on: dict[str, Any],
) -> None:
    for name, report, expected_mode in (
        ("off", off, "disabled"),
        ("on", on, "enabled"),
    ):
        if report.get("schema_version") != "cowork-eval-report.v1":
            raise SkillPairedGateError(f"{name} 不是受支持的 Cowork report")
        manifest = report.get("manifest")
        if not isinstance(manifest, dict):
            raise SkillPairedGateError(f"{name} 缺少 manifest")
        if manifest.get("suite_sha256") != _sha256(suite_path):
            raise SkillPairedGateError(f"{name} 的 suite SHA256 不匹配")
        reproducibility = manifest.get("reproducibility")
        if not isinstance(reproducibility, dict) or reproducibility.get("git_dirty") is not False:
            raise SkillPairedGateError(f"{name} 必须来自 Git clean 工作树")
        runtime = manifest.get("config", {}).get("runtime", {})
        if runtime.get("skills_mode") != expected_mode:
            raise SkillPairedGateError(
                f"{name} skills_mode 应为 {expected_mode}，实际 {runtime.get('skills_mode')}"
            )
        if not runtime.get("skills_root_sha256"):
            raise SkillPairedGateError(f"{name} 缺少冻结 Skill root fingerprint")
    if _sanitized_config(off) != _sanitized_config(on):
        raise SkillPairedGateError("两臂除 skills_mode 外的受控配置不一致")
    if off.get("suite") != suite.get("name") or on.get("suite") != suite.get("name"):
        raise SkillPairedGateError("报告 suite name 不匹配")


def _arm_points(report: dict[str, Any]) -> dict[str, ArmPoint]:
    points: dict[str, ArmPoint] = {}
    items = report.get("items")
    if not isinstance(items, list):
        raise SkillPairedGateError("Cowork report.items 必须是数组")
    for item in items:
        if not isinstance(item, dict):
            raise SkillPairedGateError("Cowork item 非法")
        item_id = str(item.get("item_id") or "")
        if not item_id or item_id in points:
            raise SkillPairedGateError(f"item_id 为空或重复: {item_id!r}")
        observation = item.get("observation")
        score = item.get("score")
        if not isinstance(observation, dict) or not isinstance(score, dict):
            raise SkillPairedGateError(f"{item_id}: observation/score 缺失")
        trace = observation.get("tool_trace")
        if not isinstance(trace, list):
            raise SkillPairedGateError(f"{item_id}: tool_trace 缺失")
        errors = 0
        loaded: list[str] = []
        for call in trace:
            if not isinstance(call, dict):
                raise SkillPairedGateError(f"{item_id}: tool trace 非法")
            if call.get("status") == "failed":
                errors += 1
            if call.get("name") == "load_skill" and call.get("status") == "ok":
                arguments = call.get("arguments")
                if isinstance(arguments, dict) and isinstance(arguments.get("name"), str):
                    loaded.append(arguments["name"])
        points[item_id] = ArmPoint(
            success=score.get("task_success") is True,
            guardrail=score.get("guardrail_pass") is True,
            tokens=int(observation.get("used_tokens") or 0),
            calls=int(observation.get("used_calls") or 0),
            tool_errors=errors,
            tool_total=len(trace),
            loaded_skills=tuple(loaded),
        )
    return points


def _mean(values: list[int | float]) -> float:
    return sum(values) / len(values) if values else 0.0


def evaluate(
    *, suite_path: Path, off_report_path: Path, on_report_path: Path
) -> dict[str, Any]:
    suite = load_suite(suite_path)
    contract = _pairing_contract(suite)
    off_report = _read_json(off_report_path)
    on_report = _read_json(on_report_path)
    _validate_reports(suite_path, suite, off_report, on_report)
    off = _arm_points(off_report)
    on = _arm_points(on_report)
    expected_ids = [str(item["id"]) for item in suite["items"]]
    if set(off) != set(expected_ids) or set(on) != set(expected_ids):
        raise SkillPairedGateError("两臂 item_id 必须与 suite 完整一致")
    trigger_ids = [str(value) for value in contract["trigger_item_ids"]]
    anti_ids = [str(value) for value in contract["anti_trigger_item_ids"]]
    expected_skill = str(contract["expected_skill"])
    gate = contract["gate"]

    def rate(ids: list[str], arm: dict[str, ArmPoint], field: str) -> float:
        return _mean([float(bool(getattr(arm[item_id], field))) for item_id in ids])

    trigger_activation = _mean(
        [float(expected_skill in on[item_id].loaded_skills) for item_id in trigger_ids]
    )
    anti_activation = _mean(
        [float(bool(on[item_id].loaded_skills)) for item_id in anti_ids]
    )
    wrong_skill_ids = [
        item_id
        for item_id in expected_ids
        if any(name != expected_skill for name in on[item_id].loaded_skills)
    ]
    off_skill_ids = [item_id for item_id in expected_ids if off[item_id].loaded_skills]
    trigger_success_off = rate(trigger_ids, off, "success")
    trigger_success_on = rate(trigger_ids, on, "success")
    anti_success_off = rate(anti_ids, off, "success")
    anti_success_on = rate(anti_ids, on, "success")
    guardrail_off = rate(expected_ids, off, "guardrail")
    guardrail_on = rate(expected_ids, on, "guardrail")
    mean_tokens_off = _mean([off[item_id].tokens for item_id in expected_ids])
    mean_tokens_on = _mean([on[item_id].tokens for item_id in expected_ids])
    mean_calls_off = _mean([off[item_id].calls for item_id in expected_ids])
    mean_calls_on = _mean([on[item_id].calls for item_id in expected_ids])
    error_rate_off = sum(off[item_id].tool_errors for item_id in expected_ids) / max(
        1, sum(off[item_id].tool_total for item_id in expected_ids)
    )
    error_rate_on = sum(on[item_id].tool_errors for item_id in expected_ids) / max(
        1, sum(on[item_id].tool_total for item_id in expected_ids)
    )
    token_regression = (
        (mean_tokens_on - mean_tokens_off) / mean_tokens_off if mean_tokens_off else 0.0
    )
    call_regression = (
        (mean_calls_on - mean_calls_off) / mean_calls_off if mean_calls_off else 0.0
    )

    boot = paired_bootstrap(
        {
            "task_success": MetricSamples(
                baseline=tuple(RatioPoint(float(off[item_id].success), 1.0) for item_id in expected_ids),
                candidate=tuple(RatioPoint(float(on[item_id].success), 1.0) for item_id in expected_ids),
            ),
            "guardrail_pass": MetricSamples(
                baseline=tuple(RatioPoint(float(off[item_id].guardrail), 1.0) for item_id in expected_ids),
                candidate=tuple(RatioPoint(float(on[item_id].guardrail), 1.0) for item_id in expected_ids),
            ),
        },
        seed=20260828,
        resamples=5000,
    )
    violations: list[dict[str, Any]] = []

    def require(ok: bool, rule: str, detail: str, case_ids: list[str] | None = None) -> None:
        if not ok:
            violations.append({"rule": rule, "detail": detail, "case_ids": case_ids or []})

    require(
        trigger_activation + 1e-12 >= float(gate["minimum_trigger_activation_rate"]),
        "trigger_activation",
        f"{trigger_activation:.3f} < {float(gate['minimum_trigger_activation_rate']):.3f}",
        [item_id for item_id in trigger_ids if expected_skill not in on[item_id].loaded_skills],
    )
    require(
        anti_activation <= float(gate["maximum_anti_trigger_activation_rate"]) + 1e-12,
        "anti_trigger_activation",
        f"{anti_activation:.3f} > {float(gate['maximum_anti_trigger_activation_rate']):.3f}",
        [item_id for item_id in anti_ids if on[item_id].loaded_skills],
    )
    require(not wrong_skill_ids, "wrong_skill_activation", "加载了非目标 Skill", wrong_skill_ids)
    require(not off_skill_ids, "disabled_arm_isolation", "disabled 臂仍加载了 Skill", off_skill_ids)
    require(
        trigger_success_on - trigger_success_off + 1e-12
        >= float(gate["minimum_trigger_success_gain"]),
        "trigger_success_regression",
        f"{trigger_success_off:.3f} -> {trigger_success_on:.3f}",
    )
    require(
        anti_success_on + 1e-12 >= anti_success_off,
        "anti_trigger_quality_regression",
        f"{anti_success_off:.3f} -> {anti_success_on:.3f}",
    )
    require(
        guardrail_on + 1e-12 >= guardrail_off,
        "guardrail_regression",
        f"{guardrail_off:.3f} -> {guardrail_on:.3f}",
    )
    require(
        token_regression <= float(gate["maximum_token_regression"]) + 1e-12,
        "token_regression",
        f"relative regression {token_regression:.3f}",
    )
    require(
        call_regression <= float(gate["maximum_call_regression"]) + 1e-12,
        "call_regression",
        f"relative regression {call_regression:.3f}",
    )
    require(
        error_rate_on - error_rate_off
        <= float(gate["maximum_tool_error_rate_increase"]) + 1e-12,
        "tool_error_rate_regression",
        f"{error_rate_off:.3f} -> {error_rate_on:.3f}",
    )

    review = suite_review(suite)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": not violations,
        "status": "passed" if not violations else "failed",
        "claim_scope": (
            "product_quality" if review["status"] == "approved" else "engineering_only_no_product_claim"
        ),
        "suite": suite["name"],
        "suite_sha256": _sha256(suite_path),
        "expected_skill": expected_skill,
        "case_count": len(expected_ids),
        "source_reports": {
            "disabled_sha256": _sha256(off_report_path),
            "enabled_sha256": _sha256(on_report_path),
        },
        "metrics": {
            "trigger_activation_rate": trigger_activation,
            "anti_trigger_activation_rate": anti_activation,
            "trigger_task_success": {"disabled": trigger_success_off, "enabled": trigger_success_on},
            "anti_trigger_task_success": {"disabled": anti_success_off, "enabled": anti_success_on},
            "guardrail_pass": {"disabled": guardrail_off, "enabled": guardrail_on},
            "mean_tokens": {"disabled": mean_tokens_off, "enabled": mean_tokens_on, "relative_delta": token_regression},
            "mean_calls": {"disabled": mean_calls_off, "enabled": mean_calls_on, "relative_delta": call_regression},
            "tool_error_rate": {"disabled": error_rate_off, "enabled": error_rate_on},
            "paired_bootstrap": {name: value.to_dict() for name, value in boot.items()},
        },
        "violations": violations,
    }


def render_markdown(report: dict[str, Any]) -> str:
    if report.get("status") == "refused":
        return (
            "# Skill paired gate：⚠️ 拒绝判定\n\n"
            "输入报告不完整或 provenance 不可比；没有生成质量通过/失败结论。\n"
        )
    verdict = "✅ 通过" if report["passed"] else "❌ 阻断"
    metrics = report["metrics"]
    lines = [
        f"# Skill paired gate：{verdict}",
        "",
        f"- Suite：`{report['suite']}`（{report['case_count']} 条）",
        f"- Skill：`{report['expected_skill']}`",
        f"- Claim scope：`{report['claim_scope']}`",
        "",
        "| 指标 | disabled | enabled |",
        "|---|---:|---:|",
        f"| trigger success | {metrics['trigger_task_success']['disabled']:.3f} | {metrics['trigger_task_success']['enabled']:.3f} |",
        f"| anti-trigger success | {metrics['anti_trigger_task_success']['disabled']:.3f} | {metrics['anti_trigger_task_success']['enabled']:.3f} |",
        f"| guardrail | {metrics['guardrail_pass']['disabled']:.3f} | {metrics['guardrail_pass']['enabled']:.3f} |",
        f"| mean tokens | {metrics['mean_tokens']['disabled']:.1f} | {metrics['mean_tokens']['enabled']:.1f} |",
        f"| mean calls | {metrics['mean_calls']['disabled']:.2f} | {metrics['mean_calls']['enabled']:.2f} |",
        f"| tool error rate | {metrics['tool_error_rate']['disabled']:.3f} | {metrics['tool_error_rate']['enabled']:.3f} |",
        "",
        f"Trigger activation：{metrics['trigger_activation_rate']:.3f}；anti-trigger activation：{metrics['anti_trigger_activation_rate']:.3f}。",
    ]
    if report["violations"]:
        lines.extend(["", "## 阻断项", ""])
        lines.extend(f"- `{item['rule']}`：{item['detail']}" for item in report["violations"])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="检查 Skill enabled/disabled 成对报告")
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--disabled-report", type=Path, required=True)
    parser.add_argument("--enabled-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    try:
        report = evaluate(
            suite_path=args.suite,
            off_report_path=args.disabled_report,
            on_report_path=args.enabled_report,
        )
    except SkillPairedGateError as error:
        report = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "passed": False,
            "status": "refused",
            "reason_code": "input_not_comparable",
            "detail_sha256": hashlib.sha256(str(error).encode("utf-8")).hexdigest(),
        }
        (args.output_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (args.output_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
        print(json.dumps({"status": "refused", "detail": str(error)}, ensure_ascii=False))
        raise SystemExit(2) from error
    (args.output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "output": str(args.output_dir)}, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
