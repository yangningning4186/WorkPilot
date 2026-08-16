"""执行真实固定综述并导出一个可人工复核的 agent_task 种子。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text

from app.agent.budget import BudgetedGateway, BudgetMeter
from app.agent.persistence import load_latest_checkpoint
from app.agent.review_graph import initialize_review_state, run_readonly_review
from app.agent.review_tools import DatabaseModelReviewTools
from app.agent.state import BudgetState
from app.agent.write_note import resume_review_after_human, review_resume_token
from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.llm.audit import SqlLlmCallAudit
from app.llm.gateway import build_model_gateway
from app.services.model_budget import build_cost_guard
from app.services.runs import create_run, ensure_conversation, get_run

DOCUMENTS = [
    (UUID("019fffcc-61b3-7434-9429-97be812f3f39"), "AGENTBENCH: EVALUATING LLMS AS AGENTS"),
    (
        UUID("019fffd7-fe61-76ec-bdd3-d50337f20da3"),
        "Agent-World: Scaling Real-World Environment Synthesis for Evolving General Agent Intelligence",
    ),
    (UUID("019fffd2-da7c-795c-95a5-8b3649e3dd5f"), "GAIA:"),
]
GOAL = "比较 AgentBench、Agent-World 与 GAIA 的评测对象、环境设计、核心发现和局限，形成中文综述。"
OUTPUT_PATH = "reviews/agent-evaluation-landscape.md"
REQUIRED_ARTIFACT_TERMS = ["AGENTBENCH", "Agent-World", "GAIA"]


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


async def _assert_documents_exist() -> None:
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT d.id, d.title
                    FROM documents d
                    JOIN document_versions v ON v.document_id = d.id
                    WHERE d.id = ANY(:ids) AND d.deleted_at IS NULL
                      AND v.activated_at IS NOT NULL AND v.invalid_at IS NULL
                      AND v.parse_status = 'done'
                    """
                ),
                {"ids": [item[0] for item in DOCUMENTS]},
            )
        ).all()
    actual = {UUID(str(row[0])): str(row[1]) for row in rows}
    missing = [str(document_id) for document_id, _ in DOCUMENTS if document_id not in actual]
    if missing:
        raise RuntimeError(f"agent_task 种子文档不可用: {missing}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="真实 agent_task 种子执行器")
    parser.add_argument("--label", required=True)
    parser.add_argument("--run-id", type=UUID)
    parser.add_argument(
        "--output-root", type=Path, default=Path("eval/outputs/agent-task-seeds")
    )
    args = parser.parse_args()
    package = (args.output_root / args.label).resolve()
    artifact_root = package / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)

    settings = Settings()
    await _assert_documents_exist()
    budget: BudgetState = {
        "max_tokens": settings.run_budget_tokens,
        "used_tokens": 0,
        "max_calls": settings.run_budget_calls,
        "used_calls": 0,
        "max_wall_ms": settings.run_budget_wall_ms,
        "used_wall_ms": 0,
        "started_at_ms": 0,
    }
    meter = BudgetMeter(
        budget, chars_per_token=settings.cost_estimate_chars_per_token
    )

    async with session_factory() as session:
        if args.run_id is None:
            conversation_id = await ensure_conversation(
                session, title=f"agent_task seed {args.label}"
            )
            run = await create_run(
                session,
                conversation_id=conversation_id,
                goal=GOAL,
                budget_tokens=budget["max_tokens"],
                budget_calls=budget["max_calls"],
                budget_wall_ms=budget["max_wall_ms"],
                workflow_type="literature_review",
            )
            await initialize_review_state(
                session,
                run_id=run.id,
                document_ids=[item[0] for item in DOCUMENTS],
                output_path=OUTPUT_PATH,
            )
            gateway = build_model_gateway(
                settings,
                audit_sink=SqlLlmCallAudit(session),
                budget_guard=build_cost_guard(settings, session_factory),
                run_id=run.id,
            )
            try:
                state = await run_readonly_review(
                    session,
                    run_id=run.id,
                    tools=DatabaseModelReviewTools(
                        session, BudgetedGateway(gateway, meter)
                    ),
                    meter=meter,
                )
            finally:
                await gateway.aclose()
            if state["status"] != "waiting_human":
                raise RuntimeError(f"固定综述没有到 HITL: {state['status']}")
            state = await resume_review_after_human(
                session,
                run_id=run.id,
                resume_token=review_resume_token(run.id),
                approved=True,
                output_root=artifact_root,
                worker_id=f"eval:{args.label}",
            )
        else:
            recovered_run = await get_run(session, args.run_id)
            checkpoint = await load_latest_checkpoint(session, run_id=args.run_id)
            if recovered_run is None or checkpoint is None:
                raise RuntimeError(f"无法恢复 agent_task 源 run: {args.run_id}")
            run = recovered_run
            conversation_id = run.conversation_id
            state = checkpoint.state
        if state["status"] != "done":
            raise RuntimeError(f"固定综述没有完成: {state['status']}")

        attempts = (
            (
                await session.execute(
                    text(
                        """
                        SELECT aa.node, aa.tool_name, aa.tool_args, aa.tool_result,
                               aa.status, aa.idempotency_key, aa.attempt_no
                        FROM agent_attempts aa
                        JOIN agent_plan_steps ps ON ps.id = aa.plan_step_id
                        WHERE aa.run_id = :run_id
                        ORDER BY ps.step_idx, aa.attempt_no
                        """
                    ),
                    {"run_id": run.id},
                )
            )
            .mappings()
            .all()
        )
        invocation = (
            (
                await session.execute(
                    text(
                        """
                        SELECT status, effect_ref, idempotency_key, retry_count
                        FROM tool_invocations WHERE run_id = :run_id
                        """
                    ),
                    {"run_id": run.id},
                )
            )
            .mappings()
            .one()
        )

    expected_tools = [
        "list_documents",
        "extract_card",
        "group_cards",
        "compare_docs",
        "generate_review",
        "write_note",
    ]
    actual_tools = [str(item["tool_name"]) for item in attempts]
    if actual_tools != expected_tools or any(item["status"] != "ok" for item in attempts):
        raise RuntimeError(f"真实工具轨迹不满足固定图: {actual_tools}")
    if invocation["status"] != "succeeded":
        raise RuntimeError("write_note 幂等调用没有成功结算")

    artifact = Path(cast("str", state["artifacts"]["note_path"]))
    artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if artifact_sha != state["artifacts"]["note_sha256"]:
        raise RuntimeError("写回产物哈希与 checkpoint 不一致")
    artifact_text = artifact.read_text(encoding="utf-8")
    missing_terms = [term for term in REQUIRED_ARTIFACT_TERMS if term not in artifact_text]
    if missing_terms:
        raise RuntimeError(f"写回产物缺少必需文献名: {missing_terms}")

    gold_tools = [
        {"name": str(item["tool_name"]), "arguments": dict(item["tool_args"] or {})}
        for item in attempts
    ]
    observed_tools = [
        {
            "name": str(item["tool_name"]),
            "arguments": dict(item["tool_args"] or {}),
            "status": str(item["status"]),
            "result": dict(item["tool_result"] or {}),
        }
        for item in attempts
    ]
    task: dict[str, Any] = {
        "schema_version": "agent-task-seed.v1",
        "id": "agent-task-seed-001",
        "category": "agent_task",
        "question": (
            "请比较 AgentBench、Agent-World 与 GAIA 的评测对象、环境设计、核心发现和局限，"
            f"生成中文综述，预览确认后写入 {OUTPUT_PATH}。"
        ),
        "gold_answer": "完成固定综述并在人工批准后写回可校验的 Markdown 产物。",
        "gold_spans": [],
        "gold_tools": gold_tools,
        "constraints": {
            "must_include": REQUIRED_ARTIFACT_TERMS,
            "must_not_include": [],
            "expected_workflow_type": "literature_review",
            "expected_artifact_sha256": artifact_sha,
            "hitl_decision": "approved",
            "candidate_review": {"status": "pending_human"},
        },
        "difficulty": 2,
        "origin": "synthetic",
        "review_status": "pending_human",
        "execution_evidence": {
            "run_id": str(run.id),
            "conversation_id": str(conversation_id),
            "run_status": state["status"],
            "used_tokens": state["budget"]["used_tokens"],
            "used_calls": state["budget"]["used_calls"],
            "artifact_path": str(artifact.relative_to(package)),
            "artifact_sha256": artifact_sha,
            "write_idempotency_key": str(invocation["idempotency_key"]),
            "write_effect_ref": str(invocation["effect_ref"]),
            "write_retry_count": int(invocation["retry_count"]),
        },
    }
    (package / "tasks.jsonl").write_text(
        json.dumps(task, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _write_json(
        package / "observed.json",
        {
            "schema_version": "agent-task-observed.v1",
            "task_id": task["id"],
            "run_id": str(run.id),
            "workflow_type": "literature_review",
            "status": state["status"],
            "hitl_decision": "approved",
            "tools": observed_tools,
            "artifact_path": str(artifact.relative_to(package)),
            "artifact_sha256": artifact_sha,
        },
    )
    _write_json(
        package / "manifest.json",
        {
            "schema_version": "agent-task-seed-manifest.v1",
            "label": args.label,
            "generated_at": datetime.now(UTC).isoformat(),
            "items": 1,
            "status": "pending_human_review",
            "source": "real_literature_review_run",
            "test_dataset_accessed": False,
            "task_ids": [task["id"]],
        },
    )
    print(json.dumps(task["execution_evidence"], ensure_ascii=False, indent=2))
    await close_database()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
