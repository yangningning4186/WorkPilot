"""夜间门禁判定（见 docs/06-评测体系.md §4.1、§4.2）。

两个子命令：

    # 1. 从一次可信的跑批导出 baseline 快照（只留数字，可提交进 git）
    PYTHONPATH=backend backend/.venv/bin/python -m eval.gate snapshot \\
      eval/outputs/dev-suite-retrieval/<run>/heading

    # 2. 用候选跑批比对 baseline，任一门禁条件不满足即非零码退出
    PYTHONPATH=backend backend/.venv/bin/python -m eval.gate check \\
      eval/outputs/dev-suite-retrieval/<run>/heading --against main

检索轨与生成轨各有一份快照，**按报告类型自动选**（`SNAPSHOT_PATHS`），
不需要每次手写 `--baseline`——手写就意味着有一天会拿生成轨的报告去比检索轨的基线。

**PR 层不跑这个。** GitHub runner 既到不了推理集群也到不了本机私人库，
PR 门禁是静态检查 + pytest（`.github/workflows/ci.yml`）。这里跑在本机/集群。

判定引擎复用 `eval.compare`：配对、兼容性校验、逐样本 delta 全部同一套口径，
本模块只在它之上加"什么算不合格"。
"""

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.compare import build_comparison
from eval.report_metrics import (
    KIND_GENERATION,
    KIND_RETRIEVAL,
    METRICS,
    LoadedReport,
    detect_kind,
    gold_span_fingerprint,
    load_report,
)

# 每条轨一份快照。文件名带轨名而不是共用 `baseline.json`：两条轨的指标集完全不同，
# 名字里看不出是哪条，就迟早有人拿生成轨的候选去比检索轨的基线。
SNAPSHOT_PATHS: dict[str, Path] = {
    KIND_RETRIEVAL: Path("eval/snapshots/retrieval.json"),
    KIND_GENERATION: Path("eval/snapshots/generation.json"),
}
SNAPSHOT_VERSION = 1

# --------------------------------------------------------------------- 门禁规则

# 规则轨指标：要求**逐样本零回退**，不是百分比余量。
# 依据是实测噪声地板（G0 台账）：light / main 档生成三次重复逐位一致，
# 四个质量指标跨度均为 0。噪声既然是 0，留百分比余量只会放过真实回退；
# 而在 20–30 条量级上一条样本就是 5pp，百分比阈值本来也表示不出来。
NO_REGRESSION_METRICS: dict[str, tuple[str, ...]] = {
    KIND_RETRIEVAL: (
        "span_recall_at_k",
        "budget_span_recall",
        "ndcg_at_k",
        "alpha_ndcg_at_k",
        "mrr",
        "context_precision",
        "refusal_correct_at_threshold",
    ),
    KIND_GENERATION: (
        "refusal_correct",
        "citation_validity_non_refusal",
        "constraint_pass",
        "constraint_pass_answerable",
        "citation_gold_alignment",
    ),
}

# 成本门禁：上涨 ≤ 20%。实测噪声最坏 8.8%（heavy 生成档），阈值大于噪声。
COST_METRIC: dict[str, str] = {
    KIND_RETRIEVAL: "retrieved_tokens",
    KIND_GENERATION: "total_tokens",
}
COST_MAX_INCREASE = 0.20

# 明确**不进**门禁的指标。写在代码里而不是留白，是为了让"顺手加回去"这个动作
# 必须先删掉一行理由。
EXCLUDED_METRICS: dict[str, str] = {
    "latency_ms": (
        "移出门禁：同配置三次重复的相对跨度已达 75.8%（G0 结论 3），"
        "远超原定的 30%；墙钟还受机器负载影响，要门禁必须先有固定机器的专门测量"
    ),
    "context_redundancy": "诊断指标，方向性参考，不作阻断条件",
    "cost_usd": "自部署价格表为 0，金额口径不可用；成本走 token 计",
}

# J2 已校准 answer_correctness-binary.v2，但当前 generation 快照还不携带 Judge 字段；
# faithfulness / citation_accuracy 也尚未各自校准。所以这里仍显式列为 pending，直到
# 首份 nightly baseline 完成字段接入。不是"忽略"，是防止误报门禁已经覆盖语义质量。
PENDING_JUDGE_METRICS: tuple[str, ...] = (
    "answer_correctness",
    "faithfulness",
    "citation_accuracy",
)

# --------------------------------------------------------- 快照字段白名单（约束 7）

# 快照要提交进 git，所以采**白名单**而不是黑名单：
# 将来给报告加字段时，新字段默认不进快照，不会悄悄把原文带进版本库。
# 生成轨报告里的 answer / gold_answer / citations[].title / span_diagnostics[].quote
# 都是逐字原文，一律不留。
_COMMON_FIELDS: tuple[str, ...] = ("item_id", "category", "answerable", "latency_ms")
_KIND_FIELDS: dict[str, tuple[str, ...]] = {
    KIND_RETRIEVAL: ("top_score", "retrieval"),
    KIND_GENERATION: (
        "refused",
        "refusal_correct",
        "total_tokens",
        "cost_usd",
        "model",
        "provider",
        "chunk_strategy",
    ),
}
# 这三个字段是嵌套 dict，其中 issues / reason 之类是自由文本，逐字段收窄
_NESTED_FIELDS: dict[str, tuple[str, ...]] = {
    "citation_validity": ("valid", "citation_count"),
    "constraint_pass": ("passed",),
    "citation_gold_alignment": ("aligned", "total"),
}
_RETRIEVED_FIELDS: tuple[str, ...] = (
    "chunk_id",
    "version_id",
    "char_start",
    "char_end",
    "content_tokens",
    "chunk_strategy",
)


class GateRefused(RuntimeError):
    """前置条件不满足，拒绝出判定结果（fail-closed，不降级成"能判多少判多少"）。"""


@dataclass(frozen=True)
class Violation:
    rule: str
    metric: str
    detail: str


@dataclass(frozen=True)
class GateOutcome:
    kind: str
    dataset: str
    item_count: int
    checks: tuple[dict[str, Any], ...]
    violations: tuple[Violation, ...]
    pending: tuple[str, ...]
    excluded: dict[str, str]

    @property
    def passed(self) -> bool:
        return not self.violations


# ------------------------------------------------------------------- 快照导出


def build_snapshot(report: LoadedReport) -> dict[str, Any]:
    """把一次跑批投影成可提交的 baseline 快照：只留数字与 UUID，不留任何原文。"""
    items = report.items
    errored = [str(item["item_id"]) for item in items if item.get("error") is not None]
    if errored:
        raise GateRefused(
            f"跑批含失败样本，不能作为 baseline: {errored[:5]}（共 {len(errored)} 条）"
        )
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "kind": report.kind,
        "dataset": report.payload.get("dataset"),
        "label": report.payload.get("label"),
        "run_id": report.payload.get("run_id"),
        "git_sha": report.payload.get("git_sha"),
        "config": report.payload.get("config"),
        "config_hash": report.payload.get("config_hash"),
        "metrics": report.payload.get("metrics"),
        "items": [_snapshot_item(item, report.kind) for item in items],
    }


def _snapshot_item(item: dict[str, Any], kind: str) -> dict[str, Any]:
    projected: dict[str, Any] = {
        field: item.get(field) for field in _COMMON_FIELDS + _KIND_FIELDS[kind]
    }
    # error 恒为 None：build_snapshot 已经拒绝了含失败样本的跑批。
    # 保留这个键是因为 _completed() / _latency() 要读它。
    projected["error"] = None
    # gold span 的身份指纹取 sha256 前 16 位：能挡住重标与解析版本漂移，不泄露 quote 原文
    projected["gold_fingerprint"] = _fingerprint(gold_span_fingerprint(item))
    if kind == KIND_RETRIEVAL:
        projected["retrieved"] = [
            {field: chunk.get(field) for field in _RETRIEVED_FIELDS}
            for chunk in item.get("retrieved") or []
        ]
        return projected
    # detect_kind 只看 citations 这个键在不在，不看内容；标题与 quote 一律不带
    projected["citations"] = [
        {"citation_id": citation.get("citation_id")}
        for citation in item.get("citations") or []
    ]
    for field, subfields in _NESTED_FIELDS.items():
        nested = item.get(field) or {}
        projected[field] = {name: nested.get(name) for name in subfields}
    return projected


def _fingerprint(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------- baseline 读取


def read_baseline(*, path: Path, git_ref: str | None) -> LoadedReport:
    """`--against <ref>` 从该 git ref 读 baseline，这样在分支上也能对着 main 的快照判。"""
    if git_ref is None:
        if not path.exists():
            raise GateRefused(
                f"baseline 快照不存在: {path}。先跑 `eval.gate snapshot` 生成并提交"
            )
        return load_report(path)
    target = f"{git_ref}:{path.as_posix()}"
    payload = json.loads(_git_show(target))
    if not isinstance(payload, dict) or not payload.get("items"):
        raise GateRefused(f"{target} 不是一份带逐样本 items 的快照")
    virtual = Path(target)
    return LoadedReport(
        path=virtual, payload=payload, kind=detect_kind(payload["items"][0], virtual)
    )


def _git_show(target: str) -> str:
    completed = subprocess.run(
        ["git", "show", target],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise GateRefused(
            f"无法从 git 读取 baseline: `git show {target}` 失败 —— "
            f"{completed.stderr.strip() or '未知错误'}"
        )
    return completed.stdout


# --------------------------------------------------------------------- 判定


def evaluate(baseline: LoadedReport, candidate: LoadedReport) -> GateOutcome:
    # 先做便宜的拒绝检查，再跑 bootstrap——否则漂移的比较也要先烧一遍重采样
    _reject_incomplete(candidate)
    _reject_gold_drift(baseline, candidate)
    comparison = _compare_or_refuse(baseline, candidate)
    kind = str(comparison["kind"])
    checks: list[dict[str, Any]] = []
    violations: list[Violation] = []
    for metric in NO_REGRESSION_METRICS[kind]:
        check, failed = _check_no_regression(comparison, metric)
        checks.append(check)
        violations.extend(failed)
    cost_check, cost_failed = _check_cost(comparison, COST_METRIC[kind])
    checks.append(cost_check)
    violations.extend(cost_failed)
    return GateOutcome(
        kind=kind,
        dataset=str(comparison["dataset"]),
        item_count=int(str(comparison["item_count"])),
        checks=tuple(checks),
        violations=tuple(violations),
        pending=PENDING_JUDGE_METRICS,
        excluded=EXCLUDED_METRICS,
    )


def _compare_or_refuse(
    baseline: LoadedReport, candidate: LoadedReport
) -> dict[str, Any]:
    """比较层的前置校验（数据集、报告类型、受控配置、item_id 配对、类别漂移）
    抛的是 `ValueError`——那是给 `eval.compare` 交互式使用的口径。门禁必须把它们
    统一成 `GateRefused`：这些同样是"拒绝判定"，不是"判为不合格"。

    否则退出码 1 会同时表示"质量回退"和"跑批配错了"，夜间自动化分不开这两件事，
    还会甩一个 traceback 而不是可执行的说明。
    """
    try:
        return build_comparison(baseline, candidate)
    except ValueError as error:
        raise GateRefused(str(error)) from error


def _reject_incomplete(candidate: LoadedReport) -> None:
    errored = [
        str(item["item_id"]) for item in candidate.items if item.get("error") is not None
    ]
    if errored:
        raise GateRefused(
            f"候选跑批含失败样本，结果不完整，拒绝判定: {errored[:5]}"
            f"（共 {len(errored)} 条）"
        )


def _reject_gold_drift(baseline: LoadedReport, candidate: LoadedReport) -> None:
    """标注漂移了就不是同一场比较；快照存的是指纹，这里逐条比指纹。"""
    left = {str(item["item_id"]): item.get("gold_fingerprint") for item in baseline.items}
    drifted = sorted(
        item_id
        for item in candidate.items
        if (item_id := str(item["item_id"])) in left
        and left[item_id] is not None
        and left[item_id] != _fingerprint(gold_span_fingerprint(item))
    )
    if drifted:
        raise GateRefused(
            f"gold span 指纹与 baseline 不一致，标注已漂移，比较无效: {drifted[:5]}"
            f"（共 {len(drifted)} 条）。重标过就要重新生成 baseline 快照"
        )


def _check_no_regression(
    comparison: dict[str, Any], metric: str
) -> tuple[dict[str, Any], list[Violation]]:
    """聚合值不许回退。

    **阻断条件是聚合值，不是逐样本。** 一开始写成"任何一条样本回退即阻断"，
    拿 E1 的 semantic 一试就发现它把净收益的改动也拦下来了（省 16.8% token、
    ndcg 上 21/57 条重排），那种门禁只会天天红、然后被习惯性忽略（§4.3）。
    逐样本的胜负数照样算出来放进报告，但它是给人看的信息，不是阻断条件。

    聚合值这条之所以敢定成"零容忍"而不是留百分比余量：噪声地板实测就是 0
    （light / main 档生成三次重复逐位一致，G0），聚合值掉了就是真掉了。
    """
    higher_is_better = _spec_direction(comparison, metric)
    improved, regressed = _sample_churn(comparison, metric, higher_is_better)
    entry = comparison["metrics"].get(metric) or {}
    baseline_value = entry.get("baseline")
    candidate_value = entry.get("candidate")
    comparable = int(entry.get("sample_size") or 0)
    check: dict[str, Any] = {
        "rule": "no_regression",
        "metric": metric,
        "comparable_samples": comparable,
        "baseline": baseline_value,
        "candidate": candidate_value,
        "improved_samples": len(improved),
        "regressed_samples": len(regressed),
        "regressed_item_ids": regressed[:10],
    }
    if comparable == 0 or baseline_value is None or candidate_value is None:
        # 快照裁字段、跑批缺指标、两侧适用性不重叠——都会让门禁静默失效，不许发生
        return check, [
            Violation(
                rule="no_comparable_samples",
                metric=metric,
                detail="该指标没有任何可配对样本，门禁形同虚设，拒绝放行",
            )
        ]
    gain = (
        candidate_value - baseline_value
        if higher_is_better
        else baseline_value - candidate_value
    )
    check["gain"] = gain
    if gain < 0:
        return check, [
            Violation(
                rule="no_regression",
                metric=metric,
                detail=(
                    f"聚合值回退 {baseline_value:.4f} → {candidate_value:.4f}"
                    f"（要求零回退，噪声地板实测为 0）；"
                    f"逐样本 {len(improved)} 胜 / {len(regressed)} 负"
                ),
            )
        ]
    return check, []


def _sample_churn(
    comparison: dict[str, Any], metric: str, higher_is_better: bool
) -> tuple[list[str], list[str]]:
    """逐样本胜负：不作阻断条件，但要放进报告——聚合持平可能掩盖两边对冲。"""
    improved: list[str] = []
    regressed: list[str] = []
    for item in comparison["items"]:
        delta = item["metrics"][metric]["delta"]
        if not delta:
            continue
        gain = delta if higher_is_better else -delta
        (improved if gain > 0 else regressed).append(str(item["item_id"]))
    return improved, regressed


def _check_cost(
    comparison: dict[str, Any], metric: str
) -> tuple[dict[str, Any], list[Violation]]:
    entry = comparison["metrics"].get(metric) or {}
    baseline_value = entry.get("baseline")
    candidate_value = entry.get("candidate")
    check: dict[str, Any] = {
        "rule": "cost_increase",
        "metric": metric,
        "baseline": baseline_value,
        "candidate": candidate_value,
        "limit": COST_MAX_INCREASE,
    }
    if baseline_value is None or candidate_value is None:
        return check, [
            Violation(
                rule="cost_unavailable",
                metric=metric,
                detail="成本指标不可用，无法判定是否用堆 token 换指标，拒绝放行",
            )
        ]
    if not baseline_value:
        check["ratio"] = None
        return check, []
    ratio = (float(candidate_value) - float(baseline_value)) / float(baseline_value)
    check["ratio"] = ratio
    if ratio > COST_MAX_INCREASE:
        return check, [
            Violation(
                rule="cost_increase",
                metric=metric,
                detail=(
                    f"上涨 {ratio:.1%}，超过 {COST_MAX_INCREASE:.0%} 上限"
                    f"（{baseline_value:.1f} → {candidate_value:.1f}）"
                ),
            )
        ]
    return check, []


def _spec_direction(comparison: dict[str, Any], metric: str) -> bool:
    for spec in METRICS[str(comparison["kind"])]:
        if spec.name == metric:
            return spec.higher_is_better
    raise GateRefused(f"门禁配置里出现未知指标: {metric}")


# --------------------------------------------------------------------- 报告


def render_markdown(outcome: GateOutcome) -> str:
    verdict = "✅ 通过" if outcome.passed else "❌ 阻断"
    lines = [
        f"# 夜间门禁：{verdict}",
        "",
        f"- 数据集：`{outcome.dataset}`（{outcome.item_count} 条，{outcome.kind} 轨）",
        "",
    ]
    if outcome.violations:
        lines += ["## 不合格项", "", "| 规则 | 指标 | 详情 |", "|---|---|---|"]
        lines += [
            f"| `{item.rule}` | `{item.metric}` | {item.detail} |"
            for item in outcome.violations
        ]
        lines.append("")
    lines += ["## 逐项检查", "", "| 规则 | 指标 | 结果 |", "|---|---|---|"]
    lines += [f"| `{check['rule']}` | `{check['metric']}` | {_check_cell(check)} |" for check in outcome.checks]
    lines += [
        "",
        "## 未启用",
        "",
        "| 指标 | 原因 |",
        "|---|---|",
    ]
    lines += [
        f"| `{metric}` | 等实验 J（Judge 校准）收口后启用 |"
        for metric in outcome.pending
    ]
    lines += [f"| `{metric}` | {reason} |" for metric, reason in outcome.excluded.items()]
    lines.append("")
    return "\n".join(lines)


def _check_cell(check: dict[str, Any]) -> str:
    if check["rule"] == "cost_increase":
        ratio = check.get("ratio")
        if ratio is None:
            return "基线为 0 或不可用，跳过比率判定"
        return f"{ratio:+.1%}（上限 {check['limit']:.0%}）"
    gain = check.get("gain")
    aggregate = "不可比" if gain is None else f"{gain:+.4f}"
    return (
        f"聚合 {aggregate}；逐样本 {check['improved_samples']} 胜 / "
        f"{check['regressed_samples']} 负（共 {check['comparable_samples']} 条可比）"
    )


# --------------------------------------------------------------------- CLI


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="夜间门禁：baseline 快照导出与判定")
    sub = parser.add_subparsers(dest="command", required=True)

    snapshot = sub.add_parser("snapshot", help="从一次跑批导出可提交的 baseline 快照")
    snapshot.add_argument("report", type=Path, help="report.json 或其所在目录")
    snapshot.add_argument(
        "--output", type=Path, default=None, help="默认按报告类型选 SNAPSHOT_PATHS"
    )

    check = sub.add_parser("check", help="用候选跑批比对 baseline 快照")
    check.add_argument("report", type=Path, help="候选 report.json 或其所在目录")
    check.add_argument(
        "--against",
        default="main",
        help="从该 git ref 读 baseline 快照；给 'working' 则读工作区文件",
    )
    check.add_argument(
        "--baseline", type=Path, default=None, help="默认按候选报告类型选快照"
    )
    check.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args(argv)


def _snapshot_path(kind: str, override: Path | None) -> Path:
    """快照路径按轨解析。给了 --baseline 就用它，否则按报告类型选。"""
    if override is not None:
        return override
    path = SNAPSHOT_PATHS.get(kind)
    if path is None:
        raise GateRefused(f"没有为 {kind} 轨定义 baseline 快照路径")
    return path


def _run_snapshot(args: argparse.Namespace) -> int:
    report = load_report(args.report)
    output = _snapshot_path(report.kind, args.output)
    snapshot = build_snapshot(report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"{report.kind} 轨 baseline 快照已写入 {output}（{len(snapshot['items'])} 条）")
    print("快照只含数字与 UUID，不含任何原文，可以提交进 git")
    return 0


def _run_check(args: argparse.Namespace) -> int:
    try:
        # 先读候选：快照路径要按它的轨来选
        candidate = load_report(args.report)
    except ValueError as error:
        # 候选报告本身读不动（缺 items、认不出轨）同样是拒判，不是不合格
        raise GateRefused(f"候选报告无法载入: {error}") from error
    git_ref = None if args.against == "working" else args.against
    baseline = read_baseline(
        path=_snapshot_path(candidate.kind, args.baseline), git_ref=git_ref
    )
    outcome = evaluate(baseline, candidate)
    report = render_markdown(outcome)
    print(report)
    if args.output_dir is not None:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "gate.md").write_text(report, encoding="utf-8")
        (args.output_dir / "gate.json").write_text(
            json.dumps(
                {
                    "passed": outcome.passed,
                    "dataset": outcome.dataset,
                    "kind": outcome.kind,
                    "item_count": outcome.item_count,
                    "checks": list(outcome.checks),
                    "violations": [vars(item) for item in outcome.violations],
                    "pending": list(outcome.pending),
                    "excluded": outcome.excluded,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0 if outcome.passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return _run_snapshot(args) if args.command == "snapshot" else _run_check(args)
    except GateRefused as error:
        # 拒绝判定与"判定为不合格"是两件事，用不同退出码区分
        print(f"门禁拒绝判定：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
