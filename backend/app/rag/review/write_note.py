"""HITL 后的 Markdown 写回，以及副作用边界上的 effectively-once 协议。"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_core.state import json_state
from app.core.run_bus import RunBus
from app.rag.review.state import ReviewState
from app.runstore.checkpoints import (
    AgentCheckpoint,
    load_latest_checkpoint,
    next_attempt_no,
    record_attempt,
    save_checkpoint,
    update_plan_step,
)
from app.runstore.invocations import (
    acquire_invocation,
    complete_invocation,
    fail_invocation,
)
from app.runstore.runs import append_events, finish_run


@dataclass(frozen=True)
class WriteNoteResult:
    path: str
    content_sha256: str
    idempotency_key: str
    reused: bool


def resolve_note_path(output_root: Path, output_path: str) -> Path:
    """只接收根目录内的相对 Markdown 路径，并拒绝现有 symlink 逃逸。"""

    relative = PurePosixPath(output_path.strip())
    if not output_path.strip() or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("output_path 必须是 Agent 输出目录内的相对路径")
    if relative.suffix.lower() != ".md":
        raise ValueError("write_note 只允许写入 .md 文件")
    root = output_root.expanduser().resolve()
    target = (root / Path(*relative.parts)).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError("output_path 不能逃逸 Agent 输出目录") from error
    return target


def _atomic_replace_markdown(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # mkdir 之后再解析一次，避免父目录中的 symlink 把目标带出根目录的初次检查结果。
    resolved_target = target.resolve(strict=False)
    if resolved_target != target:
        raise ValueError("output_path 包含不安全的符号链接")
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        temp_name = None
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)


async def write_note(
    session: AsyncSession,
    *,
    run_id: UUID,
    plan_step_id: UUID,
    output_root: Path,
    output_path: str,
    content: str,
    worker_id: str,
    lease_s: int = 30,
    after_replace: Callable[[], None] | None = None,
) -> WriteNoteResult:
    """审批后的唯一写入口；成功调用与恢复调用都返回同一个效果。"""

    if not content.strip():
        raise ValueError("不能写入空笔记")
    target = resolve_note_path(output_root, output_path)
    digest = hashlib.sha256(content.encode()).hexdigest()
    args = {"output_path": output_path, "content_sha256": digest}
    lease = await acquire_invocation(
        session,
        run_id=run_id,
        plan_step_id=plan_step_id,
        tool_name="write_note",
        args=args,
        worker_id=worker_id,
        lease_s=lease_s,
    )
    # 必须先让 in_flight 跨进程可见，再触碰文件系统。
    await session.commit()
    if not lease.acquired:
        stored = lease.result or {}
        return WriteNoteResult(
            path=str(stored.get("path") or target),
            content_sha256=str(stored.get("content_sha256") or digest),
            idempotency_key=lease.idempotency_key,
            reused=True,
        )

    try:
        # rename 后、DB succeeded 前崩溃的恢复口：内容已相同就只结算，不再改写。
        already_applied = (
            target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest
        )
        if not already_applied:
            _atomic_replace_markdown(target, content)
            if after_replace is not None:
                after_replace()
        result = {"path": str(target), "content_sha256": digest}
        await complete_invocation(
            session,
            key=lease.idempotency_key,
            worker_id=worker_id,
            result=result,
            effect_ref=f"file:{target}#sha256={digest}",
        )
        await session.commit()
    except Exception as error:
        await fail_invocation(
            session,
            key=lease.idempotency_key,
            worker_id=worker_id,
            error=str(error),
        )
        await session.commit()
        raise
    return WriteNoteResult(str(target), digest, lease.idempotency_key, already_applied)


def review_resume_token(run_id: UUID) -> str:
    return str(uuid5(run_id, "literature-review-write-confirm"))


async def resume_review_after_human(
    session: AsyncSession,
    *,
    run_id: UUID,
    resume_token: str,
    approved: bool,
    output_root: Path,
    worker_id: str,
    lease_s: int = 30,
    bus: RunBus | None = None,
) -> ReviewState:
    """消费 write_confirm；拒绝不触发副作用，批准则经幂等边界写回。"""

    if resume_token != review_resume_token(run_id):
        raise ValueError("resume_token 无效")
    checkpoint: AgentCheckpoint[ReviewState] | None = await load_latest_checkpoint(
        session, run_id=run_id
    )
    if checkpoint is None:
        raise LookupError("固定综述没有可恢复 checkpoint")
    state = checkpoint.state
    if state["status"] == "done":
        return state
    interrupt = state["interrupt"]
    if (
        state["status"] != "waiting_human"
        or state["cursor"] != 5
        or interrupt is None
        or interrupt["kind"] != "write_confirm"
        or interrupt["resume_token"] != resume_token
    ):
        raise ValueError("run 当前不在 write_note 人工确认点")

    step = state["plan"][5]
    step_id = UUID(step["id"])
    attempt_no = await next_attempt_no(
        session, run_id=run_id, plan_step_id=step_id, node="write_note"
    )
    if not approved:
        completed = json_state(deepcopy(state))
        completed["plan"][5]["status"] = "skipped"
        completed["cursor"] = 6
        completed["interrupt"] = None
        completed["status"] = "done"
        await update_plan_step(session, run_id=run_id, step_id=step_id, status="skipped")
        await record_attempt(
            session,
            run_id=run_id,
            plan_step_id=step_id,
            attempt_no=attempt_no,
            node="write_note",
            tool_name="write_note",
            tool_args={"output_path": state["output_path"]},
            status="skipped",
            error_model="用户拒绝写入；综述预览已保留。",
        )
        await save_checkpoint(
            session,
            run_id=run_id,
            state=completed,
            parent_id=checkpoint.checkpoint_id,
        )
        await append_events(
            session,
            run_id=run_id,
            events=[
                (
                    "step.update",
                    {"step_id": step["id"], "step_idx": 5, "status": "skipped"},
                ),
                (
                    "run.done",
                    {"workflow_type": "literature_review", "effect_ref": None},
                ),
            ],
        )
        await finish_run(session, run_id=run_id, status="done")
        await session.commit()
        if bus is not None:
            await bus.publish(run_id)
        return completed

    await update_plan_step(session, run_id=run_id, step_id=step_id, status="running")
    await append_events(
        session,
        run_id=run_id,
        events=[
            (
                "step.update",
                {"step_id": step["id"], "step_idx": 5, "status": "running"},
            )
        ],
    )
    await session.commit()
    if bus is not None:
        await bus.publish(run_id)

    started = time.monotonic()
    try:
        result = await write_note(
            session,
            run_id=run_id,
            plan_step_id=step_id,
            output_root=output_root,
            output_path=state["output_path"] or "",
            content=state["draft"],
            worker_id=worker_id,
            lease_s=lease_s,
        )
    except Exception as error:
        failed = json_state(deepcopy(state))
        failed["plan"][5]["status"] = "failed"
        failed["error"] = str(error)
        await update_plan_step(session, run_id=run_id, step_id=step_id, status="failed")
        await record_attempt(
            session,
            run_id=run_id,
            plan_step_id=step_id,
            attempt_no=attempt_no,
            node="write_note",
            tool_name="write_note",
            tool_args={"output_path": state["output_path"]},
            status="failed",
            latency_ms=round((time.monotonic() - started) * 1000),
            error_model=f"笔记写入失败：{error}。请修正路径或等待租约后重试。",
        )
        await save_checkpoint(
            session,
            run_id=run_id,
            state=failed,
            parent_id=checkpoint.checkpoint_id,
        )
        await append_events(
            session,
            run_id=run_id,
            events=[
                (
                    "step.update",
                    {
                        "step_id": step["id"],
                        "step_idx": 5,
                        "status": "failed",
                        "summary": str(error),
                    },
                )
            ],
        )
        await session.commit()
        if bus is not None:
            await bus.publish(run_id)
        raise

    completed_dict: dict[str, Any] = dict(json_state(deepcopy(state)))
    completed = json_state(cast("ReviewState", completed_dict))
    completed["plan"][5]["status"] = "done"
    completed["cursor"] = 6
    completed["interrupt"] = None
    completed["status"] = "done"
    completed["error"] = None
    completed["artifacts"]["note_path"] = result.path
    completed["artifacts"]["note_sha256"] = result.content_sha256
    await update_plan_step(session, run_id=run_id, step_id=step_id, status="done")
    await record_attempt(
        session,
        run_id=run_id,
        plan_step_id=step_id,
        attempt_no=attempt_no,
        node="write_note",
        tool_name="write_note",
        tool_args={"output_path": state["output_path"]},
        tool_result={"path": result.path, "reused": result.reused},
        status="ok",
        idempotency_key=result.idempotency_key,
        latency_ms=round((time.monotonic() - started) * 1000),
    )
    await save_checkpoint(
        session,
        run_id=run_id,
        state=completed,
        parent_id=checkpoint.checkpoint_id,
    )
    await append_events(
        session,
        run_id=run_id,
        events=[
            (
                "step.update",
                {"step_id": step["id"], "step_idx": 5, "status": "done"},
            ),
            (
                "artifact",
                {
                    "kind": "written_note",
                    "title": "已写入综述笔记",
                    "path": result.path,
                    "content_sha256": result.content_sha256,
                    "effect_ref": (f"file:{result.path}#sha256={result.content_sha256}"),
                    "reused": result.reused,
                },
            ),
            (
                "run.done",
                {
                    "workflow_type": "literature_review",
                    "effect_ref": f"file:{result.path}#sha256={result.content_sha256}",
                },
            ),
        ],
    )
    await finish_run(session, run_id=run_id, status="done")
    await session.commit()
    if bus is not None:
        await bus.publish(run_id)
    return completed
