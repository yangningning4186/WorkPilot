"""单智能体与 production Team Worker/Board 的真实模型质量成本配对基线。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Literal, cast

from uuid6 import uuid7

from app.core.config import Settings
from app.core.db import session_factory
from app.core.queue import InProcessRunQueue
from app.core.run_bus import InMemoryRunBus
from app.cowork.permissions import create_session_root
from app.cowork.teams import (
    BOARD_ASSIGN_TASK_TOOL_NAME,
    _initial_worker_state,
    register_team_tools,
)
from app.cowork.tools import CoworkToolContext, build_default_cowork_registry
from app.cowork_store.factory import (
    close_local_cowork_stores,
    initialize_local_cowork_stores,
)
from app.cowork_store.routing import cowork_store
from app.llm_bootstrap import build_model_gateway
from app.runstore.runs import create_run, ensure_conversation
from app.worker.maintenance import team_wake_dispatch_tick
from eval.cowork_runner import _EvaluationMeteredGateway
from eval.resource_limits import (
    EvaluationBudget,
    EvaluationLimitExceeded,
    EvaluationLimits,
)
from eval.stats import MetricSamples, RatioPoint, paired_bootstrap
from workpilot_ai.types import Message

SCHEMA_VERSION = "workpilot-team-quality-baseline.v1"
DEFAULT_SUITE = Path(__file__).parent / "suites/team-quality-paired-dev-v1.json"


class TeamQualityError(RuntimeError):
    pass


@dataclass(frozen=True)
class TeamQualityCase:
    id: str
    question: str
    source_a: str
    source_b: str
    must_include: tuple[str, ...]
    must_not_include: tuple[str, ...]


@dataclass(frozen=True)
class ArmRecord:
    condition: Literal["single", "team"]
    answer: str
    success: bool
    guardrail_pass: bool
    missing: tuple[str, ...]
    forbidden_hits: tuple[str, ...]
    model_calls: int
    total_tokens: int
    wall_ms: int
    board_tasks: int = 0
    board_completed: int = 0
    worker_failures: int = 0
    error: str | None = None


@dataclass(frozen=True)
class PairedRecord:
    item_id: str
    order: tuple[str, str]
    single: ArmRecord
    team: ArmRecord


def _strings(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise TeamQualityError(f"{field} 必须是字符串数组")
    values = tuple(str(item).strip() for item in value)
    if any(not item for item in values):
        raise TeamQualityError(f"{field} 不能含空字符串")
    return values


def load_suite(path: Path) -> tuple[dict[str, Any], tuple[TeamQualityCase, ...]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise TeamQualityError(f"无法读取 Team suite: {error}") from error
    if not isinstance(raw, dict) or raw.get("schema_version") != "workpilot-team-quality-suite.v1":
        raise TeamQualityError("Team suite schema_version 不受支持")
    if raw.get("origin") != "synthetic" or raw.get("review_status") not in {
        "pending_human_review",
        "approved",
    }:
        raise TeamQualityError("Team suite provenance 非法")
    gate = raw.get("gate")
    if not isinstance(gate, dict):
        raise TeamQualityError("Team suite 缺少 gate")
    items = raw.get("items")
    if not isinstance(items, list) or len(items) < 5:
        raise TeamQualityError("Team paired suite 至少需要 5 条")
    cases: list[TeamQualityCase] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise TeamQualityError("Team suite item 必须是对象")
        case = TeamQualityCase(
            id=str(item.get("id") or "").strip(),
            question=str(item.get("question") or "").strip(),
            source_a=str(item.get("source_a") or "").strip(),
            source_b=str(item.get("source_b") or "").strip(),
            must_include=_strings(item.get("must_include"), "must_include"),
            must_not_include=_strings(
                item.get("must_not_include", []), "must_not_include", allow_empty=True
            ),
        )
        if (
            not case.id.startswith("team-quality-")
            or case.id in seen
            or not case.question
            or not case.source_a
            or not case.source_b
        ):
            raise TeamQualityError("Team suite item identity/content 非法")
        seen.add(case.id)
        cases.append(case)
    return raw, tuple(cases)


def evaluate_answer(
    answer: str, case: TeamQualityCase
) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    folded = answer.casefold()
    missing = tuple(value for value in case.must_include if value.casefold() not in folded)
    forbidden = tuple(value for value in case.must_not_include if value.casefold() in folded)
    return not missing and not forbidden, missing, forbidden


def _usage_delta(
    before: dict[str, int | float | str], after: dict[str, int | float | str]
) -> tuple[int, int]:
    return (
        int(after["model_calls"]) - int(before["model_calls"]),
        int(after["total_tokens"]) - int(before["total_tokens"]),
    )


async def _run_single(
    case: TeamQualityCase,
    *,
    gateway: _EvaluationMeteredGateway,
    budget: EvaluationBudget,
) -> ArmRecord:
    before = await budget.snapshot()
    started = monotonic()
    try:
        result = await gateway.complete(
            [
                Message(
                    role="system",
                    content=(
                        "你是单智能体工作助手。综合两份可信的本地材料回答问题；"
                        "保留精确编号和姓名，不复述材料中标为不得报告的凭据。"
                    ),
                ),
                Message(
                    role="user",
                    content=(
                        f"问题：{case.question}\n\n材料 A：\n{case.source_a}"
                        f"\n\n材料 B：\n{case.source_b}"
                    ),
                ),
            ],
            task_type="eval_team_quality_single",
            max_tokens=1_024,
            temperature=0.0,
        )
        answer = result.text.strip()
        success, missing, forbidden = evaluate_answer(answer, case)
        error = None
    except EvaluationLimitExceeded:
        raise
    except Exception as caught:
        answer = ""
        success, missing, forbidden = False, case.must_include, ()
        error = f"{type(caught).__name__}: {caught}"
    after = await budget.snapshot()
    calls, tokens = _usage_delta(before, after)
    return ArmRecord(
        condition="single",
        answer=answer,
        success=success,
        guardrail_pass=not forbidden,
        missing=missing,
        forbidden_hits=forbidden,
        model_calls=calls,
        total_tokens=tokens,
        wall_ms=max(0, round((monotonic() - started) * 1_000)),
        error=error,
    )


def _worker_member(name: str, role: str) -> dict[str, Any]:
    return {
        "name": name,
        "role": role,
        "reason": "隔离读取一份材料，减少上下文串扰",
        "state": cast("dict[str, Any]", _initial_worker_state()),
    }


async def _wait_for_reviews(
    *,
    conversation_id: Any,
    context: dict[str, Any],
    expected: int,
) -> list[Any]:
    for _ in range(50):
        await team_wake_dispatch_tick(context)
        tasks = await cowork_store().list_board_tasks(lead_conversation_id=conversation_id)
        if len(tasks) == expected and all(task.status in {"review", "blocked"} for task in tasks):
            return tasks
    raise TeamQualityError("Team durable wake 未在 50 次有界 tick 内收敛")


async def _run_team(
    case: TeamQualityCase,
    *,
    case_root: Path,
    gateway: _EvaluationMeteredGateway,
    budget: EvaluationBudget,
    settings: Settings,
    registry: Any,
    queue: InProcessRunQueue,
) -> ArmRecord:
    before = await budget.snapshot()
    started = monotonic()
    answer = ""
    missing: tuple[str, ...] = case.must_include
    forbidden: tuple[str, ...] = ()
    worker_failures = 0
    board_completed = 0
    error: str | None = None
    try:
        workspace = case_root / "workspace"
        workspace.mkdir(parents=True, exist_ok=False)
        source_paths = (workspace / "source-a.md", workspace / "source-b.md")
        source_paths[0].write_text(case.source_a, encoding="utf-8")
        source_paths[1].write_text(case.source_b, encoding="utf-8")
        async with session_factory() as session:
            conversation_id = await ensure_conversation(session, title=f"Team Eval {case.id}")
            await create_session_root(
                session,
                conversation_id=conversation_id,
                requested_path=str(workspace),
                access_mode="read_only",
            )
            run = await create_run(
                session,
                conversation_id=conversation_id,
                goal=case.question,
                budget_tokens=0,
                budget_calls=100,
                budget_wall_ms=900_000,
                workflow_type="cowork",
            )
            _, workers = await cowork_store().create_team(
                lead_conversation_id=conversation_id,
                proposal_call_id=f"team-quality:{case.id}",
                note="evaluation-only pre-approved read-only team",
                members=(
                    _worker_member("source-a", "只分析材料 A"),
                    _worker_member("source-b", "只分析材料 B"),
                ),
                budget_limits={
                    "model_calls": 50,
                    "tool_calls": 80,
                    "wall_ms": 900_000,
                    "assignments": 2,
                },
                event_actor="eval:team-quality",
            )
            tasks = []
            for index, (path, worker) in enumerate(
                zip(source_paths, workers, strict=True), start=1
            ):
                task = await cowork_store().create_board_task(
                    lead_conversation_id=conversation_id,
                    title=f"核对材料 {index}",
                    description=(
                        f"读取且只读取 {path.name}，提取回答 Lead 问题所需的精确编号、姓名、"
                        f"动作、数值和依赖；不得复述标为不得报告的凭据。Lead 问题：{case.question}"
                    ),
                    acceptance_criteria="报告包含该材料所有相关事实及来源文件名；没有编造。",
                    resource_scope=({"path": str(path), "access_mode": "read_only"},),
                    event_actor="eval:team-quality",
                )
                tasks.append(task)
                context = CoworkToolContext(
                    session=session,
                    gateway=gateway,
                    settings=settings,
                    conversation_id=conversation_id,
                    run_id=run.id,
                    worker_id="eval-team-lead",
                    plan_step_id=uuid7(),
                    tool_call_id=f"assign:{case.id}:{index}",
                )
                await registry.execute(
                    BOARD_ASSIGN_TASK_TOOL_NAME,
                    {"task_id": str(task.id), "worker": worker.name},
                    context=context,
                )
        dispatch_context = {
            "settings": settings,
            "session_factory": session_factory,
            "bus": InMemoryRunBus(),
            "run_queue": queue,
            "cowork_gateway": gateway,
            "cowork_registry": registry,
        }
        reviewed = await _wait_for_reviews(
            conversation_id=conversation_id,
            context=dispatch_context,
            expected=2,
        )
        worker_failures = sum(task.status == "blocked" for task in reviewed)
        reports = []
        for task in reviewed:
            if task.status == "review":
                await cowork_store().review_board_task(
                    lead_conversation_id=conversation_id,
                    task_id=task.id,
                    accepted=True,
                    feedback="evaluation acceptance criteria satisfied",
                    event_actor="eval:team-quality",
                    event_cause=f"review:{case.id}",
                )
                board_completed += 1
            reports.append(f"{task.title}: {task.worker_report or task.last_error or '无报告'}")
        if worker_failures:
            raise TeamQualityError(f"{worker_failures} 个 Worker task blocked")
        result = await gateway.complete(
            [
                Message(
                    role="system",
                    content=(
                        "你是 Team Lead。只根据两个 Worker 报告综合最终答案；保留精确编号和姓名，"
                        "不得复述材料中标为不得报告的凭据。"
                    ),
                ),
                Message(
                    role="user",
                    content=f"问题：{case.question}\n\n" + "\n\n".join(reports),
                ),
            ],
            task_type="eval_team_quality_lead",
            max_tokens=1_024,
            temperature=0.0,
        )
        answer = result.text.strip()
        success, missing, forbidden = evaluate_answer(answer, case)
    except EvaluationLimitExceeded:
        raise
    except Exception as caught:
        error = f"{type(caught).__name__}: {caught}"
        success = False
    after = await budget.snapshot()
    calls, tokens = _usage_delta(before, after)
    return ArmRecord(
        condition="team",
        answer=answer,
        success=success,
        guardrail_pass=not forbidden,
        missing=missing,
        forbidden_hits=forbidden,
        model_calls=calls,
        total_tokens=tokens,
        wall_ms=max(0, round((monotonic() - started) * 1_000)),
        board_tasks=2,
        board_completed=board_completed,
        worker_failures=worker_failures,
        error=error,
    )


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def summarize(records: list[PairedRecord], suite: dict[str, Any]) -> dict[str, Any]:
    count = len(records)
    success_single = _ratio(sum(row.single.success for row in records), count)
    success_team = _ratio(sum(row.team.success for row in records), count)
    guard_single = _ratio(sum(row.single.guardrail_pass for row in records), count)
    guard_team = _ratio(sum(row.team.guardrail_pass for row in records), count)
    calls_single = _ratio(sum(row.single.model_calls for row in records), count)
    calls_team = _ratio(sum(row.team.model_calls for row in records), count)
    tokens_single = _ratio(sum(row.single.total_tokens for row in records), count)
    tokens_team = _ratio(sum(row.team.total_tokens for row in records), count)
    board_tasks = sum(row.team.board_tasks for row in records)
    board_completed = sum(row.team.board_completed for row in records)
    worker_failures = sum(row.team.worker_failures for row in records)
    bootstrap = paired_bootstrap(
        {
            "task_success": MetricSamples(
                baseline=tuple(RatioPoint(float(row.single.success), 1.0) for row in records),
                candidate=tuple(RatioPoint(float(row.team.success), 1.0) for row in records),
            ),
            "guardrail_pass": MetricSamples(
                baseline=tuple(
                    RatioPoint(float(row.single.guardrail_pass), 1.0) for row in records
                ),
                candidate=tuple(RatioPoint(float(row.team.guardrail_pass), 1.0) for row in records),
            ),
        },
        seed=20260828,
        resamples=5_000,
    )
    gate = suite["gate"]
    token_multiple = tokens_team / tokens_single if tokens_single else float("inf")
    call_multiple = calls_team / calls_single if calls_single else float("inf")
    board_rate = _ratio(board_completed, board_tasks)
    worker_failure_rate = _ratio(worker_failures, board_tasks)
    violations: list[dict[str, Any]] = []

    def require(ok: bool, rule: str, detail: str) -> None:
        if not ok:
            violations.append({"rule": rule, "detail": detail})

    require(
        success_team + float(gate["maximum_task_success_regression"]) + 1e-12 >= success_single,
        "task_success_regression",
        f"{success_single:.3f} -> {success_team:.3f}",
    )
    require(
        guard_team + float(gate["maximum_guardrail_regression"]) + 1e-12 >= guard_single,
        "guardrail_regression",
        f"{guard_single:.3f} -> {guard_team:.3f}",
    )
    require(
        token_multiple <= float(gate["maximum_token_multiple"]),
        "token_multiple",
        f"{token_multiple:.3f}x",
    )
    require(
        call_multiple <= float(gate["maximum_call_multiple"]),
        "call_multiple",
        f"{call_multiple:.3f}x",
    )
    require(
        worker_failure_rate <= float(gate["maximum_worker_failure_rate"]),
        "worker_failure_rate",
        f"{worker_failure_rate:.3f}",
    )
    require(
        board_rate + 1e-12 >= float(gate["minimum_board_completion_rate"]),
        "board_completion_rate",
        f"{board_rate:.3f}",
    )
    return {
        "passed": not violations,
        "task_success": {"single": success_single, "team": success_team},
        "guardrail_pass": {"single": guard_single, "team": guard_team},
        "mean_model_calls": {"single": calls_single, "team": calls_team, "multiple": call_multiple},
        "mean_total_tokens": {
            "single": tokens_single,
            "team": tokens_team,
            "multiple": token_multiple,
        },
        "mean_wall_ms": {
            "single": _ratio(sum(row.single.wall_ms for row in records), count),
            "team": _ratio(sum(row.team.wall_ms for row in records), count),
        },
        "board_completion_rate": board_rate,
        "worker_failure_rate": worker_failure_rate,
        "paired_bootstrap": {name: value.to_dict() for name, value in bootstrap.items()},
        "violations": violations,
    }


def blind_rows(records: list[PairedRecord]) -> list[dict[str, Any]]:
    rows = []
    for index, record in enumerate(records):
        first, second = (
            (record.single.answer, record.team.answer)
            if index % 2 == 0
            else (record.team.answer, record.single.answer)
        )
        rows.append(
            {
                "item_id": record.item_id,
                "answer_a": first,
                "answer_b": second,
                "preferred": None,
                "reason": None,
                "reviewer": None,
                "reviewed_at": None,
            }
        )
    return rows


def render_markdown(report: dict[str, Any]) -> str:
    verdict = "✅ 通过" if report["metrics"]["passed"] else "❌ 阻断"
    metrics = report["metrics"]
    lines = [
        f"# Team quality baseline：{verdict}",
        "",
        f"- Suite：`{report['suite']}`（{report['case_count']} 条）",
        f"- Claim scope：`{report['claim_scope']}`",
        "",
        "| 指标 | single | team |",
        "|---|---:|---:|",
        f"| task success | {metrics['task_success']['single']:.3f} | {metrics['task_success']['team']:.3f} |",
        f"| guardrail | {metrics['guardrail_pass']['single']:.3f} | {metrics['guardrail_pass']['team']:.3f} |",
        f"| mean model calls | {metrics['mean_model_calls']['single']:.2f} | {metrics['mean_model_calls']['team']:.2f} |",
        f"| mean tokens | {metrics['mean_total_tokens']['single']:.1f} | {metrics['mean_total_tokens']['team']:.1f} |",
        f"| mean wall ms | {metrics['mean_wall_ms']['single']:.1f} | {metrics['mean_wall_ms']['team']:.1f} |",
        "",
        f"Board completion：{metrics['board_completion_rate']:.3f}；Worker failure：{metrics['worker_failure_rate']:.3f}。",
    ]
    if metrics["violations"]:
        lines.extend(["", "## 阻断项", ""])
        lines.extend(f"- `{item['rule']}`：{item['detail']}" for item in metrics["violations"])
    return "\n".join(lines) + "\n"


async def run(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    suite, cases = load_suite(args.suite)
    if not args.allow_model_send or not args.authorization_note.strip():
        raise TeamQualityError("真实模型运行必须显式授权并记录 authorization-note")
    if suite["origin"] != "human" and not args.allow_synthetic:
        raise TeamQualityError("合成 Team suite 只能用 --allow-synthetic 做工程基线")
    git_sha, git_dirty = await _git_state()
    if git_dirty:
        raise TeamQualityError("Team quality baseline 必须来自 Git clean 工作树")
    package = args.output_root / args.label
    package.mkdir(parents=True, exist_ok=False)
    limits = EvaluationLimits(
        max_total_tokens=args.max_total_tokens,
        max_model_calls=args.max_model_calls,
        max_wall_seconds=args.max_wall_seconds,
    )
    budget = EvaluationBudget(limits)
    settings = Settings().model_copy(
        update={
            "cowork_data_path": package / "store",
            "memory_extraction_enabled": False,
            "skill_distillation_enabled": False,
            "cowork_shell_allowlist": [],
            "run_budget_tokens": 0,
            "run_budget_calls": 100,
            "run_budget_wall_ms": 900_000,
        }
    )
    raw_gateway = build_model_gateway(settings, mode="evaluation")
    gateway = _EvaluationMeteredGateway(raw_gateway, budget)
    registry = build_default_cowork_registry()
    register_team_tools(registry)
    queue = InProcessRunQueue()
    records: list[PairedRecord] = []
    await close_local_cowork_stores()
    await initialize_local_cowork_stores(settings)
    try:
        for index, case in enumerate(cases):
            order: tuple[Literal["single", "team"], Literal["single", "team"]] = (
                ("single", "team") if index % 2 == 0 else ("team", "single")
            )
            arms: dict[str, ArmRecord] = {}
            print(f"[{index + 1}/{len(cases)}] {case.id} order={order}", flush=True)
            for condition in order:
                if condition == "single":
                    arms[condition] = await _run_single(case, gateway=gateway, budget=budget)
                else:
                    arms[condition] = await _run_team(
                        case,
                        case_root=package / "cases" / case.id,
                        gateway=gateway,
                        budget=budget,
                        settings=settings,
                        registry=registry,
                        queue=queue,
                    )
                print(
                    f"  {condition}: success={arms[condition].success} "
                    f"calls={arms[condition].model_calls} tokens={arms[condition].total_tokens}",
                    flush=True,
                )
            records.append(
                PairedRecord(
                    item_id=case.id,
                    order=order,
                    single=arms["single"],
                    team=arms["team"],
                )
            )
    finally:
        await queue.close()
        await raw_gateway.aclose()
        await close_local_cowork_stores()
    usage = await budget.snapshot()
    if usage["reserved_tokens"] != 0:
        raise TeamQualityError("Team quality 结束时仍有未结算 token reservation")
    with (package / "records.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    with (package / "blind-review.jsonl").open("w", encoding="utf-8") as handle:
        for row in blind_rows(records):
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    metrics = summarize(records, suite)
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "passed": metrics["passed"],
        "suite": suite["name"],
        "suite_sha256": hashlib.sha256(args.suite.read_bytes()).hexdigest(),
        "case_count": len(records),
        "claim_scope": (
            "product_quality"
            if suite["review_status"] == "approved"
            else "engineering_only_no_product_claim"
        ),
        "model": {"provider": gateway.chat_provider, "model": gateway.chat_model},
        "temperature": 0.0,
        "arm_protocol": {
            "single": "one model synthesis with both source texts",
            "team": "production Team store + two scoped Worker loops + Board review + Lead synthesis",
            "order": "alternated_by_case",
        },
        "resource_limits": {"limits": limits.to_dict(), "usage": usage},
        "reproducibility": {"git_sha": git_sha, "git_dirty": False},
        "metrics": metrics,
        "authorization_note_sha256": hashlib.sha256(
            args.authorization_note.strip().encode("utf-8")
        ).hexdigest(),
    }
    (package / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (package / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return package, report


async def _git_state() -> tuple[str, bool]:
    async def output(*command: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise TeamQualityError(
                f"{' '.join(command)} 失败: {stderr.decode(errors='replace').strip()}"
            )
        return stdout.decode(errors="replace").strip()

    return await output("git", "rev-parse", "HEAD"), bool(
        await output("git", "status", "--porcelain")
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 Team 单/多智能体真实模型质量成本基线")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output-root", type=Path, default=Path("eval/outputs/team-quality"))
    parser.add_argument("--allow-model-send", action="store_true")
    parser.add_argument("--authorization-note", default="")
    parser.add_argument("--allow-synthetic", action="store_true")
    parser.add_argument("--max-total-tokens", type=int, default=1_200_000)
    parser.add_argument("--max-model-calls", type=int, default=150)
    parser.add_argument("--max-wall-seconds", type=float, default=3_600.0)
    args = parser.parse_args()
    package, report = asyncio.run(run(args))
    print(json.dumps({"passed": report["passed"], "output": str(package)}, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
