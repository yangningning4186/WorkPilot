"""建索引的后台作业与进度。

**为什么不进 Arq 队列。** 建索引是纯本地的活：读磁盘上的文件、调本机 embedding 端点、
写磁盘上的索引。它不碰数据库、没有跨进程的副作用、也不需要重试语义。为它加一个队列作业
类型意味着改 `RunQueue` 协议、两个队列实现、worker 注册和一张作业表——那些复杂度换来的
唯一好处是"进程重启后作业能续上"，而这件事恰好不需要：**索引要么写成了，要么没写成**，
清单里的 embedding 签名就是答案，重启后 `rebuild` 一下即可，没有中间态要恢复。

所以这里是一个进程内的作业表：每个 KB 同时只允许一个作业，状态给界面轮询。进程重启会丢
作业记录，不会丢数据——用户看到的是"这个库没建索引"，那正是事实。

**为什么要有它。** 一个文件夹的论文解析是分钟级的活，挂在 HTTP 请求上必然超时
（CLAUDE.md：worker 不依附 HTTP 连接）。而且一条不动的进度条和一个卡死的界面，在用户
看来没有区别。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

import structlog

from app.knowledge_contracts import KnowledgeUnavailableError
from app.rag.kb.service import SkippedSource

logger = structlog.get_logger(__name__)

JobStatus = Literal["running", "done", "failed"]
# 完成的作业留多久还能被查到。界面轮询间隔是秒级，十分钟足够任何一次刷新看到结果，
# 又不会让一个长期开着的进程慢慢攒满已完成作业。
COMPLETED_TTL_S = 600.0


@dataclass(frozen=True)
class IndexingJob:
    slug: str
    status: JobStatus
    # 正在做什么，直接展示给用户：「解析 attention.pdf」「建立索引」。
    stage: str
    done: int
    total: int
    started_at: float
    finished_at: float | None = None
    error: str | None = None
    skipped: tuple[SkippedSource, ...] = ()
    added: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "status": self.status,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "error": self.error,
            "added": self.added,
            "skipped": [
                {"filename": item.filename, "reason": item.reason} for item in self.skipped
            ],
        }


class KbIndexingJobs:
    """每个 KB 同时只跑一个作业。

    并发跑两个作业会让两次 `build_index` 都往同一个 `index/` 目录写——后写的赢，先写的
    那批文档静静地不见了。拒绝比合并简单，也比合并诚实。
    """

    def __init__(self) -> None:
        self._jobs: dict[str, IndexingJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def get(self, slug: str) -> IndexingJob | None:
        self._evict_expired()
        return self._jobs.get(slug)

    def is_running(self, slug: str) -> bool:
        job = self._jobs.get(slug)
        return job is not None and job.status == "running"

    def start(
        self,
        slug: str,
        work: Callable[
            [Callable[[str, int, int], None]], Awaitable[tuple[int, tuple[SkippedSource, ...]]]
        ],
        *,
        stage: str,
    ) -> IndexingJob:
        if self.is_running(slug):
            raise KnowledgeUnavailableError(f"知识库 {slug} 正在建索引，等它完成再提交下一批。")
        self._evict_expired()
        job = IndexingJob(
            slug=slug, status="running", stage=stage, done=0, total=0, started_at=time.time()
        )
        self._jobs[slug] = job
        self._tasks[slug] = asyncio.create_task(self._run(slug, work))
        return job

    def _progress(self, slug: str) -> Callable[[str, int, int], None]:
        def report(stage: str, done: int, total: int) -> None:
            current = self._jobs.get(slug)
            if current is None or current.status != "running":
                return
            self._jobs[slug] = replace(current, stage=stage, done=done, total=total)

        return report

    async def _run(
        self,
        slug: str,
        work: Callable[
            [Callable[[str, int, int], None]], Awaitable[tuple[int, tuple[SkippedSource, ...]]]
        ],
    ) -> None:
        try:
            added, skipped = await work(self._progress(slug))
        except Exception as error:
            # 建索引失败的原因几乎都是可执行的（端点没起、扫描件、路径不存在），
            # 按约束 4 原样留给界面显示；意料之外的那些留在日志里。
            logger.warning("kb.index.failed", slug=slug, exc_info=True)
            current = self._jobs.get(slug)
            self._jobs[slug] = replace(
                current if current is not None else _placeholder(slug),
                status="failed",
                error=str(error) or error.__class__.__name__,
                finished_at=time.time(),
            )
            return
        finally:
            self._tasks.pop(slug, None)
        current = self._jobs.get(slug)
        self._jobs[slug] = replace(
            current if current is not None else _placeholder(slug),
            status="done",
            stage="完成",
            added=added,
            skipped=skipped,
            finished_at=time.time(),
        )

    def _evict_expired(self) -> None:
        cutoff = time.time() - COMPLETED_TTL_S
        for slug, job in list(self._jobs.items()):
            if job.finished_at is not None and job.finished_at < cutoff:
                del self._jobs[slug]


def _placeholder(slug: str) -> IndexingJob:  # pragma: no cover - 防御
    return IndexingJob(
        slug=slug, status="running", stage="", done=0, total=0, started_at=time.time()
    )


_jobs = KbIndexingJobs()


def default_indexing_jobs() -> KbIndexingJobs:
    """进程内单例。作业表必须和跑作业的那个事件循环在同一个进程里。"""
    return _jobs


__all__ = [
    "IndexingJob",
    "KbIndexingJobs",
    "default_indexing_jobs",
]
