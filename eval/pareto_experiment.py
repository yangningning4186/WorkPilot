"""实验 C：质量-成本-延迟帕累托前沿（docs/07 §4）。

## 相对 §4 原表的三处偏离，以及为什么

§4 的 C1–C8 是按 `generate / plan / rewrite / judge` 四个环节设计的。本项目实际的
在线答题链路只有**两个**模型环节，所以矩阵按真实链路重做：

- **没有 `plan`**：固定综述是一张写死的六节点图，没有动态规划器（[11 MVP 边界]）。
  凭空造一个 plan 环节来凑满八格，测出来的也是假的。
- **`rewrite` 不在链路上**：`query_decomposition_enabled` 默认关闭，开着它等于同时
  改了两个变量。
- **`judge` 不在线上链路**：它属于评测侧，降档问题由实验 J 单独回答。

留下的两个真实环节是 **证据门控（evidence_sufficiency）** 与 **生成（grounded_answer）**，
做成 2 因子设计。C8「全外部 API」需要 external 档，未部署，会被跳过并如实记录。

## 质量轴用规则轨，不用 Judge

六类 Judge 的人工标签还没收口（[09 W4]），此刻用它当质量轴，等于把一个未校准的
测量仪器架在整张前沿图下面。所以只用确定性指标：拒答正确率、引用有效率、
引用与 gold span 的对齐率。这三个都不经过模型，重复跑必然一致。

## 成本轴是 GPU 秒与 token，不是美元

等价云单价是外部假设，填不同的数得到不同的"成本"，而"哪个配置在前沿上"只取决于
GPU 时间与 token 的相对关系。不引入不可验证的假设。

## 串行跑批，这是刻意的

现有 harness 逐条跑（共用一个 DB session，并发化要先拆 session）。§4 要求
「每个配置都要在**相同并发设定**下测」——串行同样是一个一致的设定，配置之间可比。
但由此得到的绝对成本数字是串行口径，不代表生产吞吐，报告里必须写明。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.core.config import Settings
from app.core.db import session_factory
from app.llm.batch import BatchSpec, gpu_batch
from app.llm.routing import Tier, load_routing_table, routing_env
from app.services.cost_report import load_batch_costs
from eval.generation_baseline import run_generation_baseline

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTING_PATH = REPO_ROOT / "config" / "routing.yaml"

# 只改这两个 task_type：它们是在线答题链路上仅有的两个模型环节。
GENERATE_TASK = "grounded_answer"
GATE_TASK = "evidence_sufficiency"


@dataclass(frozen=True)
class Config:
    name: str
    description: str
    generate: Tier
    gate: Tier


CONFIGS: tuple[Config, ...] = (
    Config("C1", "全重档", "heavy", "heavy"),
    Config("C2", "全主力（线上基准）", "main", "main"),
    Config("C3", "全轻档", "light", "light"),
    Config("C4", "生成降档", "light", "main"),
    Config("C5", "门控降档", "main", "light"),
    Config("C6", "生成升档", "heavy", "main"),
    Config("C7", "门控升档", "main", "heavy"),
    Config("C8", "全外部 API", "external", "external"),
)


@dataclass
class ConfigOutcome:
    config: Config
    repeat: int
    skipped_reason: str | None = None
    failed_reason: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)


def build_routing_document(config: Config) -> dict[str, Any]:
    """在仓库那份 routing.yaml 上改两行路由，其余原样保留。

    刻意从真实配置派生而不是另写一份：另写一份会让实验里的 timeout、
    `enable_thinking`、fallback 链与线上悄悄分叉，测出来的就不是线上那套东西。
    """

    document = yaml.safe_load(ROUTING_PATH.read_text(encoding="utf-8"))
    document["routes"][GENERATE_TASK] = config.generate
    document["routes"][GATE_TASK] = config.gate
    # 升档会在配置之外偷偷换档位，整个矩阵就不是受控实验了。
    document.pop("escalation", None)
    return document


def available_tiers(settings: Settings) -> set[Tier]:
    table = load_routing_table(ROUTING_PATH, routing_env(settings))
    return {name for name, spec in table.tiers.items() if spec.primary.available}


async def run_one(
    config: Config,
    repeat: int,
    *,
    dataset: str,
    origin: str,
    top_k: int,
    theta: float,
    output_root: Path,
    settings: Settings,
    gate_max_tokens: int,
) -> ConfigOutcome:
    label = f"pareto-{config.name}-r{repeat}"
    routing_path = output_root / "routing" / f"{config.name}-r{repeat}.yaml"
    routing_path.parent.mkdir(parents=True, exist_ok=True)
    routing_path.write_text(
        yaml.safe_dump(build_routing_document(config), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    # 改配置不改代码：整个矩阵靠换 routing.yaml 切换，这正是路由表存在的理由。
    #
    # 门控 token 预算对**所有配置统一放宽**：默认的 300 是按非推理模型调的，
    # heavy 会把它全烧在思考链上、返回空 content。在一个某档位结构上够不到的预算下
    # 比档位，比的是预算不是档位。放宽是受控的——每个配置拿到同一个值，
    # 而多花的 token 会如实出现在 tok/题 这一列里。
    run_settings = settings.model_copy(
        update={
            "routing_config_path": routing_path,
            "evidence_gate_max_tokens": gate_max_tokens,
        }
    )

    table = load_routing_table(routing_path, routing_env(run_settings))
    generate_model = table.tiers[config.generate].primary.model

    outcome = ConfigOutcome(config=config, repeat=repeat)
    async with session_factory() as session:
        spec = BatchSpec(
            tier=config.generate,
            model=generate_model,
            label=label,
            node_count=run_settings.gpu_node_count,
            gpu_model=run_settings.gpu_model.strip() or None,
        )
        async with gpu_batch(session, spec) as batch_id:
            try:
                result = await run_generation_baseline(
                    dataset_name=dataset,
                    label=label,
                    origin=origin,
                    top_k=top_k,
                    theta=theta,
                    output_root=output_root / "runs",
                    settings=run_settings,
                )
            except Exception as error:  # 一个配置跑不动，不该带走整个矩阵
                # "这个档位根本做不了这个任务"本身就是实验结果，如实记下来继续跑。
                # 注意评测模式已关掉 fallback，所以这里看到的是该档位的真实能力边界，
                # 而不是被 fallback 悄悄兜住之后的假象（docs/07 §7.4）。
                outcome.failed_reason = f"{type(error).__name__}: {error}"
                print(f"[失败] {label}：{outcome.failed_reason[:200]}")
                return outcome

    async with session_factory() as session:
        # 按 batch_id 而不是 label：重跑实验会产生同名批次，按 label 取第一条
        # 会拿到上一次（可能是被中断的那次）的数据，而且从结果里完全看不出来。
        costs = await load_batch_costs(session, batch_id=batch_id)

    if result.report_path is not None:
        # report_path 指向 report.md（给人看的），机器读的指标在同目录的 report.json。
        report = json.loads(
            (result.report_path.parent / "report.json").read_text(encoding="utf-8")
        )
        outcome.metrics = report["metrics"]
    if costs:
        item = costs[0]
        outcome.cost = {
            "batch_id": str(batch_id),
            "wall_s": str(item.wall_s),
            "gpu_s": str(item.gpu_s),
            "gpu_s_per_task": str(item.gpu_s_per_task),
            "llm_calls": item.task_count,
            "total_tokens": item.total_tokens,
            "output_tokens": item.output_tokens,
            "tokens_per_task": item.tokens_per_task,
            "mean_concurrency": str(item.mean_concurrency),
        }
    return outcome


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def summarize(outcomes: list[ConfigOutcome]) -> str:
    """按配置聚合三次重复取中位数。

    只看 r1 会把单次抖动（或一条脏数据）当成结论——§4 要求 3 次重复正是为此。
    中位数而不是均值：三次里有一次异常时，中位数不会被它拖走。
    """

    by_config: dict[str, list[ConfigOutcome]] = {}
    for outcome in outcomes:
        by_config.setdefault(outcome.config.name, []).append(outcome)
    merged: list[ConfigOutcome] = []
    for group in by_config.values():
        usable = [o for o in group if o.metrics and o.cost]
        if not usable:
            merged.append(group[0])
            continue
        head = usable[0]
        merged.append(
            ConfigOutcome(
                config=head.config,
                repeat=0,
                metrics=head.metrics,
                cost={
                    **head.cost,
                    "wall_s": f"{_median([float(o.cost['wall_s']) for o in usable]):.1f}",
                    "repeats": len(usable),
                },
            )
        )
    return _render(merged)


def _render(outcomes: list[ConfigOutcome]) -> str:
    header = (
        f"{'配置':<5}{'generate':<9}{'gate':<9}{'拒答':>8}{'引用有效':>9}{'引用对齐':>9}"
        f"{'约束':>8}{'错误':>6}{'墙钟s中位':>10}{'tok/题':>9}{'延迟ms':>9}{'重复':>5}"
    )
    lines = [header, "-" * len(header)]
    for outcome in outcomes:
        config = outcome.config
        if outcome.skipped_reason is not None:
            lines.append(
                f"{config.name:<5}{config.generate:<9}{config.gate:<9}"
                f"  — 跳过：{outcome.skipped_reason}"
            )
            continue
        if outcome.failed_reason is not None:
            lines.append(
                f"{config.name:<5}{config.generate:<9}{config.gate:<9}"
                f"  — 跑不动：{outcome.failed_reason[:120]}"
            )
            continue
        metrics = outcome.metrics
        lines.append(
            f"{config.name:<5}{config.generate:<9}{config.gate:<9}"
            f"{_pct(_dig(metrics, 'refusal', 'accuracy')):>8}"
            f"{_pct(_dig(metrics, 'citation_validity', 'non_refusal_rate')):>9}"
            f"{_pct(_dig(metrics, 'citation_gold_alignment', 'rate')):>9}"
            f"{_pct(_dig(metrics, 'constraint_pass', 'rate')):>8}"
            f"{metrics.get('error_count', 0):>6}"
            f"{outcome.cost.get('wall_s', '—'):>10}"
            f"{outcome.cost.get('tokens_per_task', '—'):>9}"
            f"{_num(_dig(metrics, 'latency_ms', 'mean')):>9}"
            f"{outcome.cost.get('repeats', '—'):>5}"
        )
    lines.append("")
    lines.append("配置说明：" + "；".join(f"{c.name}={c.description}" for c in CONFIGS))
    lines.append("质量轴只用规则轨（拒答正确率 / 引用有效率 / 引用对齐率 / 约束通过率），")
    lines.append("未使用尚未校准的 Judge。引用对齐是自动代理指标，不等于语义引用准确率。")
    lines.append("成本轴是 GPU 秒与 token，未做美元折算。串行跑批，绝对数字不代表生产吞吐。")
    lines.append("证据门控 token 预算对所有配置统一放宽，避免比的是预算而不是档位。")
    return "\n".join(lines)


def _dig(metrics: dict[str, Any], *path: str) -> object:
    current: object = metrics
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _num(value: object) -> str:
    if not isinstance(value, int | float):
        return "—"
    return f"{value:.0f}"


def _pct(value: object) -> str:
    if not isinstance(value, int | float):
        return "—"
    return f"{value * 100:.1f}%"


async def main() -> None:
    parser = argparse.ArgumentParser(description="实验 C：质量-成本帕累托前沿")
    parser.add_argument("--dataset", default="core-dev")
    parser.add_argument("--origin", default="human")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--theta", type=float, default=0.35)
    parser.add_argument(
        "--gate-max-tokens",
        type=int,
        default=1024,
        help="证据门控的 token 预算，对所有配置统一生效（默认 300 对推理模型不够）",
    )
    parser.add_argument(
        "--configs", default="", help="只跑其中几个，逗号分隔，例如 C2,C3"
    )
    parser.add_argument("--output-root", type=Path, default=Path("eval/outputs/pareto"))
    args = parser.parse_args()

    settings = Settings()
    tiers = available_tiers(settings)
    selected = [
        config
        for config in CONFIGS
        if not args.configs or config.name in {n.strip() for n in args.configs.split(",")}
    ]

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_root = args.output_root / timestamp
    output_root.mkdir(parents=True, exist_ok=True)

    outcomes: list[ConfigOutcome] = []
    for config in selected:
        missing = {config.generate, config.gate} - tiers
        if missing:
            outcomes.append(
                ConfigOutcome(
                    config=config,
                    repeat=0,
                    skipped_reason=f"档位未部署：{', '.join(sorted(missing))}",
                )
            )
            print(f"[跳过] {config.name} {config.description}：档位未部署 {sorted(missing)}")
            continue
        for repeat in range(1, args.repeats + 1):
            print(f"[跑批] {config.name} r{repeat} generate={config.generate} gate={config.gate}")
            outcome = await run_one(
                config,
                repeat,
                dataset=args.dataset,
                origin=args.origin,
                top_k=args.top_k,
                theta=args.theta,
                output_root=output_root,
                settings=settings,
                gate_max_tokens=args.gate_max_tokens,
            )
            outcomes.append(outcome)

    payload = [
        {
            "config": outcome.config.name,
            "description": outcome.config.description,
            "generate_tier": outcome.config.generate,
            "gate_tier": outcome.config.gate,
            "repeat": outcome.repeat,
            "skipped_reason": outcome.skipped_reason,
            "failed_reason": outcome.failed_reason,
            "metrics": outcome.metrics,
            "cost": outcome.cost,
        }
        for outcome in outcomes
    ]
    (output_root / "matrix.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = summarize(outcomes)
    (output_root / "summary.txt").write_text(summary, encoding="utf-8")
    print()
    print(summary)
    print()
    print(f"完整矩阵：{output_root / 'matrix.json'}")


if __name__ == "__main__":
    asyncio.run(main())
