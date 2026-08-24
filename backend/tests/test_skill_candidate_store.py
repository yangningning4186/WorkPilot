"""候选与作业队列落在目录里之后的行为约束。

这些用例过去要开一个 PostgreSQL 会话；现在只要一个 tmp_path，所以它们不再带
`pytest.mark.integration`——这是搬进目录顺带买到的东西。
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from uuid6 import uuid7

from app.cowork.skills.candidate_store import (
    SkillCandidateStoreError,
    claim_skill_job,
    complete_skill_job,
    get_skill_candidate,
    list_dispatchable_skill_jobs,
    list_skill_candidates,
    retry_or_fail_skill_job,
    schedule_skill_distillation,
    set_candidate_status,
    upsert_skill_candidate,
)
from app.cowork.skills.distillation import successful_tool_names

SKILL_MD = "---\nname: learned-summarize-report\ndescription: 整理报告\n---\n\n1. 读取报告\n"


def test_successful_tool_names_keeps_only_tools_that_reported_ok() -> None:
    state = {
        "final_message": "已生成摘要。",
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "ok-call",
                        "type": "function",
                        "function": {"name": "read_text_file", "arguments": "{}"},
                    },
                    {
                        "id": "failed-call",
                        "type": "function",
                        "function": {"name": "run_shell", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "ok-call", "content": '{"ok":true}'},
            {"role": "tool", "tool_call_id": "failed-call", "content": '{"ok":false}'},
        ],
    }

    assert successful_tool_names(state) == ["read_text_file"]


def test_job_is_idempotent_per_run_and_carries_its_own_source(tmp_path: Path) -> None:
    run_id = uuid7()
    first = schedule_skill_distillation(
        tmp_path,
        run_id=run_id,
        goal="整理预算并输出报告",
        final_message="报告已生成。",
        successful_tools=["read_text_file", "create_artifact"],
    )
    second = schedule_skill_distillation(
        tmp_path,
        run_id=run_id,
        goal="换一个说法",
        final_message="不该覆盖。",
        successful_tools=[],
    )
    assert first is not None and second is not None
    # 重复入队不能改写来源快照，也不能把重试计数清零。
    assert second.goal == "整理预算并输出报告"
    assert second.successful_tools == ["read_text_file", "create_artifact"]


def test_lease_blocks_a_second_worker_until_it_expires(tmp_path: Path) -> None:
    run_id = uuid7()
    schedule_skill_distillation(
        tmp_path, run_id=run_id, goal="g", final_message="m", successful_tools=["read_text_file"]
    )
    claimed = claim_skill_job(
        tmp_path, run_id=run_id, worker_id="worker-1", lease_s=300, max_attempts=3
    )
    assert claimed is not None and claimed.attempts == 1
    assert (
        claim_skill_job(tmp_path, run_id=run_id, worker_id="worker-2", lease_s=300, max_attempts=3)
        is None
    )
    # 租约里的 claimed_at 才是过期依据；mtime 会被备份和同步工具改掉，不能用。
    lock = tmp_path / ".queue" / f"{run_id}.lock"
    stale = datetime.now(UTC) - timedelta(seconds=600)
    lock.write_text(json.dumps({"worker_id": "worker-1", "claimed_at": stale.isoformat()}))
    stolen = claim_skill_job(
        tmp_path, run_id=run_id, worker_id="worker-2", lease_s=300, max_attempts=3
    )
    assert stolen is not None and stolen.attempts == 2
    # 租约已经易主，原持有者不能再把作业标记完成。
    assert complete_skill_job(tmp_path, run_id=run_id, worker_id="worker-1") is False
    assert complete_skill_job(tmp_path, run_id=run_id, worker_id="worker-2") is True
    assert not (tmp_path / ".queue" / f"{run_id}.json").exists()


def test_exhausted_job_is_archived_instead_of_blocking_the_queue(tmp_path: Path) -> None:
    run_id = uuid7()
    schedule_skill_distillation(
        tmp_path, run_id=run_id, goal="g", final_message="m", successful_tools=["read_text_file"]
    )
    for _ in range(2):
        assert (
            claim_skill_job(tmp_path, run_id=run_id, worker_id="w", lease_s=300, max_attempts=2)
            is not None
        )
        retry_or_fail_skill_job(
            tmp_path, run_id=run_id, worker_id="w", error="模型超时", max_attempts=2
        )
        # 退避会把 available_at 推到将来，重排后立刻拉回以便下一轮重试。
        path = tmp_path / ".queue" / f"{run_id}.json"
        if path.exists():
            payload = json.loads(path.read_text())
            payload["available_at"] = datetime.now(UTC).isoformat()
            path.write_text(json.dumps(payload))
    assert not (tmp_path / ".queue" / f"{run_id}.json").exists()
    assert (tmp_path / ".queue" / f"{run_id}.failed.json").exists()
    assert list_dispatchable_skill_jobs(tmp_path, max_attempts=2, lease_s=300) == []
    # 留档的失败作业不会被下一次完成重新入队，否则它会永远重跑。
    assert (
        schedule_skill_distillation(
            tmp_path, run_id=run_id, goal="g", final_message="m", successful_tools=["x"]
        )
        is None
    )


def test_dispatcher_skips_live_leases_and_returns_expired_ones(tmp_path: Path) -> None:
    live, expired = uuid7(), uuid7()
    for run_id in (live, expired):
        schedule_skill_distillation(
            tmp_path, run_id=run_id, goal="g", final_message="m", successful_tools=["t"]
        )
        assert (
            claim_skill_job(tmp_path, run_id=run_id, worker_id="w", lease_s=300, max_attempts=3)
            is not None
        )
    stale = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    (tmp_path / ".queue" / f"{expired}.lock").write_text(
        json.dumps({"worker_id": "w", "claimed_at": stale})
    )

    assert list_dispatchable_skill_jobs(tmp_path, max_attempts=3, lease_s=300) == [(expired, 1)]


def test_evidence_counts_distinct_runs_and_survives_a_reused_run(tmp_path: Path) -> None:
    first_run, second_run = uuid7(), uuid7()
    kwargs = {
        "capability_key": "summarize-report",
        "suggested_name": "learned-summarize-report",
        "description": "整理报告",
        "skill_md": SKILL_MD,
        "tools": ["read_text_file"],
        "confidence": 0.9,
    }
    candidate = upsert_skill_candidate(tmp_path, run_id=first_run, **kwargs)
    assert candidate.evidence_count == 1
    assert upsert_skill_candidate(tmp_path, run_id=first_run, **kwargs).evidence_count == 1
    assert upsert_skill_candidate(tmp_path, run_id=second_run, **kwargs).evidence_count == 2
    # 一个 run 一个空文件：计数是 listdir，写入是 O_EXCL，不需要读-改-写。
    evidence = tmp_path / "summarize-report" / "evidence"
    assert {path.name for path in evidence.iterdir()} == {str(first_run), str(second_run)}


def test_confidence_never_regresses_and_decided_candidates_keep_their_text(
    tmp_path: Path,
) -> None:
    run_id = uuid7()
    base = {
        "capability_key": "summarize-report",
        "suggested_name": "learned-summarize-report",
        "description": "整理报告",
        "skill_md": SKILL_MD,
        "tools": ["read_text_file"],
    }
    upsert_skill_candidate(tmp_path, run_id=run_id, confidence=0.9, **base)
    lowered = upsert_skill_candidate(tmp_path, run_id=uuid7(), confidence=0.4, **base)
    # 一次表述不佳的蒸馏不该把已经攒够的分数打回去。
    assert lowered.confidence == pytest.approx(0.9)

    set_candidate_status(
        tmp_path, capability_key="summarize-report", status="rejected", review_reason="用户已拒绝"
    )
    after = upsert_skill_candidate(
        tmp_path,
        run_id=uuid7(),
        capability_key="summarize-report",
        suggested_name="learned-something-else",
        description="换了个说法",
        skill_md="---\nname: learned-something-else\ndescription: 换了个说法\n---\n\n1. 别的\n",
        tools=["run_shell"],
        confidence=0.95,
    )
    # 人已经对着这份内容做过决定，模型不能在背后把它换掉；但证据仍然累加。
    assert after.status == "rejected"
    assert after.suggested_name == "learned-summarize-report"
    assert after.skill_md == SKILL_MD
    assert after.tools == ["read_text_file"]
    assert after.evidence_count == 3


def test_hand_edited_candidate_body_is_what_gets_read_back(tmp_path: Path) -> None:
    upsert_skill_candidate(
        tmp_path,
        run_id=uuid7(),
        capability_key="summarize-report",
        suggested_name="learned-summarize-report",
        description="整理报告",
        skill_md=SKILL_MD,
        tools=["read_text_file"],
        confidence=0.9,
    )
    edited = SKILL_MD.replace("1. 读取报告", "1. 读取报告\n2. 人工补的一步")
    (tmp_path / "summarize-report" / "SKILL.md").write_text(edited, encoding="utf-8")

    candidate = get_skill_candidate(tmp_path, "summarize-report")
    assert candidate is not None and candidate.skill_md == edited


def test_listing_puts_review_first_and_rejects_traversal(tmp_path: Path) -> None:
    for key, status in [
        ("alpha-flow", "promoted"),
        ("beta-flow", "needs_review"),
        ("gamma-flow", "collecting"),
    ]:
        upsert_skill_candidate(
            tmp_path,
            run_id=uuid7(),
            capability_key=key,
            suggested_name=f"learned-{key}",
            description=key,
            skill_md=f"---\nname: learned-{key}\ndescription: {key}\n---\n\n1. 做事\n",
            tools=["read_text_file"],
            confidence=0.9,
        )
        set_candidate_status(tmp_path, capability_key=key, status=status)  # type: ignore[arg-type]

    assert [item.capability_key for item in list_skill_candidates(tmp_path)] == [
        "beta-flow",
        "gamma-flow",
        "alpha-flow",
    ]
    # capability_key 会变成目录名，越界必须在拼路径之前就被拒。
    with pytest.raises(SkillCandidateStoreError):
        get_skill_candidate(tmp_path, "../../etc")
