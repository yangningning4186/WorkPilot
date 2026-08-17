"""实验 J：Judge 能否从 heavy 降到 main（口径 B —— 判者间一致性）。

**口径 B 不问"谁判得对"，只问"两档判得一样不一样"。**
选它是因为口径 A（各自与人工标签比）当前跑不出结论：唯一一份 70 条标签是
助手起草、作者复核的（`human-labels-provenance.json` 自己写明 QWK 会被系统性高估），
而 heavy 在那份标签上已经是 1.0000，main 最好也只能打平——没有分辨空间。

判者间一致性不需要新标签就能支撑成本决策：如果 main 与 heavy 的判决高度一致，
那么"用 main 当 Judge"改变的只是成本，不改变结论。**这不能证明两者都判得准**，
只能证明两者判得一样；准不准仍要等独立人工标签，那是口径 A 的事。

同一批预测还顺带回答 G0 留下的推测：Judge 输出是"短离散量"，
是否像 C7 的证据门控那样即使在 heavy 上也可复现。

    PYTHONPATH=backend backend/.venv/bin/python -m eval.judge_downgrade \\
      --examples <bundle>/examples.jsonl \\
      --run heavy=<...>/judge-predictions.jsonl --run heavy-r2=<...>/heavy-r2.jsonl \\
      --run main=<...>/main-r1.jsonl \\
      --baseline heavy --candidate main \\
      --output-dir eval/outputs/judge-downgrade/<label>
"""

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from eval.judge_calibration import (
    agreement_metrics,
    bootstrap_agreement,
    load_examples,
    load_judge_predictions,
)


@dataclass(frozen=True)
class JudgeRun:
    name: str
    path: Path
    scores: dict[str, int]
    models: tuple[str, ...]
    input_tokens: int
    output_tokens: int


def load_runs(specs: Sequence[str], examples_path: Path) -> list[JudgeRun]:
    examples = load_examples(examples_path)
    runs: list[JudgeRun] = []
    for spec in specs:
        name, _, raw_path = spec.partition("=")
        if not name or not raw_path:
            raise ValueError(f"--run 需要 name=path 形式，收到: {spec}")
        path = Path(raw_path)
        predictions = load_judge_predictions(path, examples)
        runs.append(
            JudgeRun(
                name=name,
                path=path,
                scores={key: row.score for key, row in predictions.items()},
                models=tuple(sorted({f"{row.provider}/{row.model}" for row in predictions.values()})),
                input_tokens=sum(row.input_tokens for row in predictions.values()),
                output_tokens=sum(row.output_tokens for row in predictions.values()),
            )
        )
    ids = {frozenset(run.scores) for run in runs}
    if len(ids) != 1:
        raise ValueError("各次跑批的 example 集合不一致，无法配对比较")
    return runs


def pairwise(runs: Sequence[JudgeRun]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for left, right in combinations(runs, 2):
        keys = sorted(left.scores)
        a = [left.scores[key] for key in keys]
        b = [right.scores[key] for key in keys]
        metrics = agreement_metrics(a, b)
        disagreements = [key for key in keys if left.scores[key] != right.scores[key]]
        rows.append(
            {
                "left": left.name,
                "right": right.name,
                # 同一档位的两次重复 = 可复现性；跨档位 = 判者间一致性。
                # 两者数值同源但含义完全不同，报告里必须分开读。
                "kind": "reproducibility" if _same_tier(left, right) else "inter_judge",
                **metrics,
                "bootstrap": bootstrap_agreement(a, b),
                "disagreement_ids": disagreements,
            }
        )
    return rows


def _same_tier(left: JudgeRun, right: JudgeRun) -> bool:
    return left.models == right.models


def build_report(
    runs: Sequence[JudgeRun], *, baseline: str, candidate: str
) -> dict[str, Any]:
    names = {run.name for run in runs}
    for name in (baseline, candidate):
        if name not in names:
            raise ValueError(f"--baseline/--candidate 指向不存在的 run: {name}")
    rows = pairwise(runs)
    headline = next(
        row
        for row in rows
        if {row["left"], row["right"]} == {baseline, candidate}
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "design": "口径 B：判者间一致性；不声称任何一方判得更准",
        "baseline": baseline,
        "candidate": candidate,
        "runs": [
            {
                "name": run.name,
                "models": list(run.models),
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
                "sample_count": len(run.scores),
                "label_distribution": _distribution(run),
            }
            for run in runs
        ],
        "headline": headline,
        "pairwise": rows,
    }


def _distribution(run: JudgeRun) -> dict[str, int]:
    counts: dict[str, int] = {}
    for score in run.scores.values():
        counts[str(score)] = counts.get(str(score), 0) + 1
    return dict(sorted(counts.items()))


def markdown(payload: dict[str, Any]) -> str:
    head = payload["headline"]
    lines = [
        "# 实验 J · Judge 降档（口径 B：判者间一致性）",
        "",
        f"> {payload['design']}",
        "",
        "## 各次跑批",
        "",
        "| run | 模型 | 样本 | 标签分布 | 输入 token | 输出 token |",
        "|---|---|---:|---|---:|---:|",
    ]
    for run in payload["runs"]:
        lines.append(
            f"| `{run['name']}` | {'、'.join(run['models'])} | {run['sample_count']} | "
            f"{run['label_distribution']} | {run['input_tokens']:,} | {run['output_tokens']:,} |"
        )
    lines += [
        "",
        "## 两两一致性",
        "",
        "| 对比 | 类型 | 一致率 | QWK | 分歧数 |",
        "|---|---|---:|---:|---:|",
    ]
    for row in payload["pairwise"]:
        kind = "同档重复" if row["kind"] == "reproducibility" else "跨档"
        qwk = "未定义" if row["qwk"] is None else f"{row['qwk']:.4f}"
        lines.append(
            f"| `{row['left']}` vs `{row['right']}` | {kind} | {row['accuracy']:.4f} | "
            f"{qwk} | {len(row['disagreement_ids'])} |"
        )
    qwk = "未定义" if head["qwk"] is None else f"{head['qwk']:.4f}"
    lines += [
        "",
        f"**主结论**：`{payload['baseline']}` 与 `{payload['candidate']}` 一致率 "
        f"{head['accuracy']:.4f}，QWK {qwk}，分歧 {len(head['disagreement_ids'])} 条。",
        "",
        "> 一致 ≠ 都判得准。这个口径只能支撑成本决策；",
        "> 判得准不准要等独立人工标签（口径 A）。",
        "",
    ]
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="实验 J：Judge 降档的判者间一致性")
    parser.add_argument("--examples", type=Path, required=True)
    parser.add_argument("--run", action="append", required=True, help="name=path")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    runs = load_runs(args.run, args.examples)
    payload = build_report(runs, baseline=args.baseline, candidate=args.candidate)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report = markdown(payload)
    (args.output_dir / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
