"""建索引作业表的用例。

这张表存在的唯一理由是"建索引是分钟级的活，不能挂在 HTTP 请求上"。它必须做对三件事：
同一个库不能并发建（后写的会静静盖掉先写的）、失败要变成可展示的状态而不是一个消失的
task、完成的作业要能被界面轮询到。
"""

from __future__ import annotations

import asyncio

import pytest

from app.knowledge_contracts import KnowledgeUnavailableError
from app.rag.kb.jobs import KbIndexingJobs
from app.rag.kb.service import SkippedSource


async def _settle() -> None:
    """让作业协程跑到它下一个挂起点。"""
    for _ in range(8):
        await asyncio.sleep(0)


async def test_job_reports_progress_then_completion() -> None:
    jobs = KbIndexingJobs()
    gate = asyncio.Event()

    async def work(progress: object) -> tuple[int, tuple[SkippedSource, ...]]:
        progress("解析 attention.pdf", 1, 3)  # type: ignore[operator]
        await gate.wait()
        return 3, ()

    jobs.start("papers", work, stage="准备导入 3 个文件")  # type: ignore[arg-type]
    await _settle()

    running = jobs.get("papers")
    assert running is not None
    assert running.status == "running"
    assert running.stage == "解析 attention.pdf"
    assert (running.done, running.total) == (1, 3)

    gate.set()
    await _settle()

    done = jobs.get("papers")
    assert done is not None
    assert done.status == "done"
    assert done.added == 3


async def test_two_jobs_on_one_kb_are_refused_not_merged() -> None:
    """并发跑两个作业会让两次 build_index 往同一个 index/ 目录写。

    后写的赢，先写的那批文档静静地不见了——而且没有任何一处会报错。拒绝比合并诚实。
    """
    jobs = KbIndexingJobs()
    gate = asyncio.Event()

    async def work(_progress: object) -> tuple[int, tuple[SkippedSource, ...]]:
        await gate.wait()
        return 1, ()

    jobs.start("papers", work, stage="第一批")  # type: ignore[arg-type]
    await _settle()

    with pytest.raises(KnowledgeUnavailableError, match="正在建索引"):
        jobs.start("papers", work, stage="第二批")  # type: ignore[arg-type]

    # 另一个库不受影响。
    jobs.start("notes", work, stage="别的库")  # type: ignore[arg-type]
    gate.set()
    await _settle()


async def test_failure_becomes_a_displayable_status_not_a_vanished_task() -> None:
    """作业跑在后台，抛出去没有人接。不落成状态的话，界面会永远显示"正在建索引"。"""
    jobs = KbIndexingJobs()

    async def work(_progress: object) -> tuple[int, tuple[SkippedSource, ...]]:
        raise KnowledgeUnavailableError("embedding 端点调用失败，确认本机推理服务已启动")

    jobs.start("papers", work, stage="准备导入")  # type: ignore[arg-type]
    await _settle()

    failed = jobs.get("papers")
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error is not None
    assert "确认本机推理服务已启动" in failed.error
    # 失败之后必须能重试。
    assert not jobs.is_running("papers")


async def test_skipped_files_survive_into_the_finished_job() -> None:
    """跳过了哪几篇、为什么，是用户唯一能看到的交代。"""
    jobs = KbIndexingJobs()

    async def work(_progress: object) -> tuple[int, tuple[SkippedSource, ...]]:
        return 2, (SkippedSource(filename="scan.pdf", reason="没有文本层"),)

    jobs.start("papers", work, stage="准备导入")  # type: ignore[arg-type]
    await _settle()

    job = jobs.get("papers")
    assert job is not None
    assert job.to_dict()["skipped"] == [{"filename": "scan.pdf", "reason": "没有文本层"}]


async def test_no_job_for_an_untouched_kb() -> None:
    assert KbIndexingJobs().get("papers") is None
