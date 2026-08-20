"""杀真实 Arq worker，验证固定综述从 checkpoint 恢复且不重放副作用。

这不是单元测试的替代品——单测里的"崩溃"是抛异常，进程还活着，`finally` 还会跑。
这里用 `SIGKILL` 打掉整个进程组：没有 `finally`、没有优雅关闭、连接直接断，
数据库那边看到的就是一个租约挂在半空的 run。只有这样才能证明 watchdog 与
`claim_run` 的条件 UPDATE 在真实失联下确实接得住。

跑法（需要 postgres + redis + 可用的 tier_main 端点）：

    PYTHONPATH=backend backend/.venv/bin/python -m eval.worker_recovery_demo \
      --document-id <uuid> --document-id <uuid> \
      --goal "比较两篇文档的方法差异" --kill-after-steps 2

产出 `eval/outputs/worker-recovery/<label>/report.json`，含击杀时刻的进度、恢复次数、
两个 worker 各自执行过的节点，以及写回文件的 inode/mtime 对照。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import close_database, session_factory
from app.core.queue import get_run_queue
from app.rag.review.graph import initialize_review_state
from app.runstore.runs import create_run, ensure_conversation

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
# 租约必须远短于默认的 60s, 否则等 watchdog 发现失联要等一分钟, 演示没法看。
DEMO_LEASE_S = "12"
DEMO_HEARTBEAT_S = "4"


class DemoFailure(RuntimeError):
    """演示未能证明目标性质；fail-closed，不出"看起来成功"的报告。"""


@dataclass
class WorkerWindow:
    """一个 worker 进程活着期间发生的事。"""

    worker_id: str
    pid: int
    killed: bool
    nodes_completed: list[str]


@dataclass
class RecoveryReport:
    label: str
    run_id: str
    document_ids: list[str]
    output_path: str
    killed_after_steps: int
    plan_at_kill: list[str]
    recovery_count: int
    windows: list[WorkerWindow]
    replayed_nodes: list[str]
    note_inode: int | None
    note_mtime_ns: int | None
    note_bytes: int | None
    final_status: str
    duration_s: float
    generated_at: str


def _worker_env(settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND_DIR)
    env["RUN_LEASE_S"] = DEMO_LEASE_S
    env["RUN_HEARTBEAT_S"] = DEMO_HEARTBEAT_S
    return env


def _spawn_worker(settings: Settings, log_path: Path) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("wb")
    return subprocess.Popen(
        [sys.executable, "-m", "arq", "app.worker.main.WorkerSettings"],
        cwd=BACKEND_DIR,
        env=_worker_env(settings),
        stdout=handle,
        stderr=subprocess.STDOUT,
        # 自成进程组, 这样 killpg 能一次带走 arq 及其子进程, 不留孤儿继续续租。
        start_new_session=True,
    )


def _sigkill(process: subprocess.Popen[bytes]) -> None:
    """SIGKILL 整个进程组。SIGTERM 会触发优雅关闭, 那就不是"失联"了。"""

    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    process.wait(timeout=15)


async def _completed_nodes(session: AsyncSession, run_id: UUID) -> list[tuple[str, str]]:
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT node, status
                    FROM agent_attempts
                    WHERE run_id = :run_id
                    ORDER BY created_at
                    """
                ),
                {"run_id": run_id},
            )
        )
        .mappings()
        .all()
    )
    return [(str(row["node"]), str(row["status"])) for row in rows]


async def _run_snapshot(session: AsyncSession, run_id: UUID) -> tuple[str, int]:
    row = (
        (
            await session.execute(
                text("SELECT status, recovery_count FROM agent_runs WHERE id = :run_id"),
                {"run_id": run_id},
            )
        )
        .mappings()
        .one()
    )
    return str(row["status"]), int(row["recovery_count"])


async def _plan_status(session: AsyncSession, run_id: UUID) -> list[str]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT status FROM agent_plan_steps WHERE run_id = :run_id "
                    "ORDER BY step_idx"
                ),
                {"run_id": run_id},
            )
        )
        .scalars()
        .all()
    )
    return [str(item) for item in rows]


async def _wait_for(
    predicate_sql: str,
    params: dict[str, object],
    *,
    timeout_s: float,
    what: str,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        async with session_factory() as session:
            hit = (await session.execute(text(predicate_sql), params)).scalar_one()
        if hit:
            return
        await asyncio.sleep(0.5)
    raise DemoFailure(f"等待超时（{timeout_s}s）：{what}")


async def run_demo(
    *,
    document_ids: list[UUID],
    goal: str,
    output_path: str,
    kill_after_steps: int,
    label: str,
    output_root: Path,
) -> RecoveryReport:
    if len(set(document_ids)) < 2:
        raise DemoFailure("固定综述至少需要两篇不同文档")
    if not 1 <= kill_after_steps <= 4:
        # 第 5 步之后就进 HITL 了, 那不是"执行中失联"。
        raise DemoFailure("kill_after_steps 需要落在 1..4，才是执行中途失联")

    settings = Settings()
    started = time.monotonic()
    package = output_root / label
    package.mkdir(parents=True, exist_ok=True)

    async with session_factory() as session:
        conversation_id = await ensure_conversation(session)
        run = await create_run(
            session,
            conversation_id=conversation_id,
            goal=goal,
            budget_tokens=settings.run_budget_tokens,
            budget_calls=settings.run_budget_calls,
            budget_wall_ms=settings.run_budget_wall_ms,
            workflow_type="literature_review",
        )
        await initialize_review_state(
            session,
            run_id=run.id,
            document_ids=document_ids,
            output_path=output_path,
        )
        await session.commit()
    run_id = run.id

    queue = await get_run_queue()
    await queue.enqueue_review_run(run_id)

    windows: list[WorkerWindow] = []
    first = _spawn_worker(settings, package / "worker-1.log")
    try:
        # 等到指定步数完成再下手, 保证击杀点确实在"已有进度"之后。
        await _wait_for(
            "SELECT count(*) >= :n FROM agent_plan_steps "
            "WHERE run_id = :run_id AND status = 'done'",
            {"run_id": run_id, "n": kill_after_steps},
            timeout_s=300,
            what=f"前 {kill_after_steps} 步完成",
        )
        async with session_factory() as session:
            plan_at_kill = await _plan_status(session, run_id)
            before = await _completed_nodes(session, run_id)
        _sigkill(first)
        windows.append(
            WorkerWindow(
                worker_id="worker-1",
                pid=first.pid,
                killed=True,
                nodes_completed=[node for node, status in before if status == "ok"],
            )
        )
    except Exception:
        if first.poll() is None:
            _sigkill(first)
        raise

    # 第二个 worker 只负责跑 watchdog 与恢复后的执行, 它没有任何"上一个 worker"的内存。
    second = _spawn_worker(settings, package / "worker-2.log")
    try:
        await _wait_for(
            "SELECT recovery_count > 0 FROM agent_runs WHERE id = :run_id",
            {"run_id": run_id},
            timeout_s=180,
            what="watchdog 发现失联并重新入队",
        )
        await _wait_for(
            "SELECT status = 'waiting_human' FROM agent_runs WHERE id = :run_id",
            {"run_id": run_id},
            timeout_s=600,
            what="恢复后跑到人工确认点",
        )
        async with session_factory() as session:
            after = await _completed_nodes(session, run_id)
            final_status, recovery_count = await _run_snapshot(session, run_id)
    finally:
        if second.poll() is None:
            _sigkill(second)

    windows.append(
        WorkerWindow(
            worker_id="worker-2",
            pid=second.pid,
            killed=False,
            nodes_completed=[
                node for node, status in after[len(before) :] if status == "ok"
            ],
        )
    )

    # 核心断言: 击杀前已经 ok 的节点, 恢复后不能再出现第二条 ok。
    done_before = [node for node, status in before if status == "ok"]
    done_after = [node for node, status in after[len(before) :] if status == "ok"]
    replayed = sorted(set(done_before) & set(done_after))
    if replayed:
        raise DemoFailure(f"恢复后重跑了已完成节点：{replayed}")
    if recovery_count < 1:
        raise DemoFailure("run 没有被 watchdog 自动恢复过")
    if final_status != "waiting_human":
        raise DemoFailure(f"恢复后未到达人工确认点，实际状态 {final_status}")

    # 只读阶段不应该写任何笔记——写回在 HITL 之后。
    note = (settings.agent_output_path / output_path).resolve()
    stat = note.stat() if note.exists() else None
    if stat is not None:
        raise DemoFailure(f"人工确认前不应存在写回文件：{note}")

    return RecoveryReport(
        label=label,
        run_id=str(run_id),
        document_ids=[str(item) for item in document_ids],
        output_path=output_path,
        killed_after_steps=kill_after_steps,
        plan_at_kill=plan_at_kill,
        recovery_count=recovery_count,
        windows=windows,
        replayed_nodes=replayed,
        note_inode=None if stat is None else stat.st_ino,
        note_mtime_ns=None if stat is None else stat.st_mtime_ns,
        note_bytes=None if stat is None else stat.st_size,
        final_status=final_status,
        duration_s=round(time.monotonic() - started, 2),
        generated_at=datetime.now(UTC).isoformat(),
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="杀真实 worker 的 checkpoint 恢复演示")
    parser.add_argument("--document-id", action="append", required=True, type=UUID)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--output-path", default=None)
    parser.add_argument("--kill-after-steps", type=int, default=2)
    parser.add_argument("--label", default=None)
    parser.add_argument("--output-root", type=Path, default=Path("eval/outputs/worker-recovery"))
    args = parser.parse_args()

    label = args.label or f"recovery-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    output_path = args.output_path or f"recovery-demo/{uuid4()}.md"
    try:
        report = await run_demo(
            document_ids=list(args.document_id),
            goal=args.goal,
            output_path=output_path,
            kill_after_steps=args.kill_after_steps,
            label=label,
            output_root=args.output_root,
        )
    except DemoFailure as error:
        print(f"演示未通过：{error}", file=sys.stderr)
        return 1
    finally:
        await close_database()

    package = args.output_root / label
    package.mkdir(parents=True, exist_ok=True)
    (package / "report.json").write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
