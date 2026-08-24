"""后台 shell 任务：跑得比一次工具调用久的命令。

`run_shell` 是同步的：起进程、等它结束、把输出交回去。dev server、长构建、watch 进程
在这个模型下要么撞超时，要么把整个 run 卡住。后台任务把"启动"和"取输出"拆成两步，
模型可以先去做别的事，再回来轮询。

**后台进程活在 worker 进程里，不落库。** 它和 `persistent_session=true` 的 PTY 是两种
不同工具：后台任务适合 dev server、watch 与长构建；持久 PTY 适合需要连续 `cd`、
`export` 或激活 venv 的一串短命令。PTY 重启后会从最后 cwd 重建并明确报告 env 丢失；
后台任务则不会重放，`shell_task_output` 会直接说任务不在了，避免重复外部副作用。

任务按 conversation 隔离：拿着别的会话的 task_id 读不到输出，和浏览器 session 同一个道理。
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from uuid6 import uuid7

from app.cowork.shell import ShellCommand, _minimal_environment, _terminate_process_group


class ShellTaskError(RuntimeError):
    """面向模型的后台任务错误（约束 4：message 是可执行指令）。"""


@dataclass(frozen=True)
class ShellTaskSnapshot:
    task_id: str
    command: str
    cwd: str
    running: bool
    exit_code: int | None
    output: str
    output_truncated: bool
    elapsed_s: float


@dataclass
class _ShellTask:
    task_id: str
    conversation_id: UUID
    command: str
    cwd: str
    process: asyncio.subprocess.Process
    started_at: float
    max_output_bytes: int
    buffer: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    # 已经交给模型的字节数：轮询只回增量，否则每次都把整段历史再塞一遍上下文。
    delivered: int = 0
    readers: tuple[asyncio.Task[None], ...] = ()
    expiry: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self.process.returncode is None

    def absorb(self, chunk: bytes) -> None:
        room = self.max_output_bytes - len(self.buffer)
        if room > 0:
            self.buffer.extend(chunk[:room])
        if len(chunk) > room:
            self.truncated = True

    def snapshot(self, *, incremental: bool) -> ShellTaskSnapshot:
        start = self.delivered if incremental else 0
        chunk = bytes(self.buffer[start:])
        if incremental:
            self.delivered = len(self.buffer)
        return ShellTaskSnapshot(
            task_id=self.task_id,
            command=self.command,
            cwd=self.cwd,
            running=self.running,
            exit_code=self.process.returncode,
            output=chunk.decode("utf-8", errors="replace"),
            output_truncated=self.truncated,
            elapsed_s=round(time.monotonic() - self.started_at, 3),
        )


class CoworkShellTaskManager:
    """按会话隔离的后台进程表。生命周期跟 worker 走，由 ctx 持有。"""

    def __init__(
        self,
        *,
        max_tasks_per_conversation: int,
        output_max_bytes: int,
        hard_ttl_s: float,
        terminate_grace_s: float,
        max_tasks_total: int | None = None,
        max_retained_tasks: int | None = None,
    ) -> None:
        if max_tasks_per_conversation < 1:
            raise ValueError("每会话后台任务上限必须大于 0")
        derived_total = max(8, max_tasks_per_conversation * 4)
        resolved_total = derived_total if max_tasks_total is None else max_tasks_total
        if resolved_total < max_tasks_per_conversation:
            raise ValueError("后台任务全局上限不能小于每会话上限")
        resolved_retained = (
            max(16, resolved_total * 4) if max_retained_tasks is None else max_retained_tasks
        )
        if resolved_retained < resolved_total:
            raise ValueError("后台任务保留上限不能小于全局运行上限")
        self._tasks: dict[str, _ShellTask] = {}
        self._lock = asyncio.Lock()
        self._max_tasks = max_tasks_per_conversation
        self._max_tasks_total = resolved_total
        self._max_retained_tasks = resolved_retained
        self._output_max_bytes = output_max_bytes
        self._hard_ttl_s = hard_ttl_s
        self._terminate_grace_s = terminate_grace_s

    async def start(
        self,
        *,
        conversation_id: UUID,
        command: ShellCommand,
        cwd: Path,
    ) -> ShellTaskSnapshot:
        async with self._lock:
            await self._reap_locked()
            self._prune_completed_locked(reserve=1)
            live = [
                task
                for task in self._tasks.values()
                if task.conversation_id == conversation_id and task.running
            ]
            if len(live) >= self._max_tasks:
                raise ShellTaskError(
                    f"本会话同时运行的后台任务已达上限 {self._max_tasks}，"
                    "请先用 shell_task_kill 结束不再需要的任务"
                )
            live_total = sum(task.running for task in self._tasks.values())
            if live_total >= self._max_tasks_total:
                raise ShellTaskError(
                    f"后台任务全局运行数已达上限 {self._max_tasks_total}，"
                    "请先用 shell_task_kill 结束不再需要的任务"
                )
            process = await self._spawn(command, cwd)
            task = _ShellTask(
                task_id=str(uuid7()),
                conversation_id=conversation_id,
                command=command.raw,
                cwd=str(cwd),
                process=process,
                started_at=time.monotonic(),
                max_output_bytes=self._output_max_bytes,
            )
            # stdout/stderr 合流：模型关心的是"发生了什么"，分两路只会让它更难拼时序。
            task.readers = tuple(
                asyncio.create_task(self._pump(task, stream))
                for stream in (process.stdout, process.stderr)
                if stream is not None
            )
            self._tasks[task.task_id] = task
            # hard TTL 必须主动触发。只在下一次 start 时顺手清理，会让“启动后再也不操作”
            # 的 dev server 永久活着，名义上的上限因此形同虚设。
            task.expiry = asyncio.create_task(
                self._expire(task),
                name=f"cowork-shell-expiry-{task.task_id}",
            )
            return task.snapshot(incremental=True)

    async def read(
        self, *, conversation_id: UUID, task_id: str, full: bool = False
    ) -> ShellTaskSnapshot:
        async with self._lock:
            task = self._require(conversation_id, task_id)
            return task.snapshot(incremental=not full)

    async def wait(
        self,
        *,
        conversation_id: UUID,
        task_id: str,
        timeout_s: float,
        cancel_event: asyncio.Event | None = None,
    ) -> ShellTaskSnapshot:
        """挂在某个后台任务的结束事件上，直到它退出、超时或本次 run 被取消。

        这是"轮询"的替代品：`sleep` + `shell_task_output` 每转一圈都要花一次模型调用，
        等一个十分钟的构建就是几十次往返，而且醒来的时刻和任务结束的时刻永远对不齐。

        它**不**把 run 挂起成 sleeping。挂起会释放 worker，而进程活在这个 worker 的内存
        里——换一个 worker 恢复就再也读不到输出了。等待期间 worker 心跳照常续租，模型
        调用为零，所以烧掉的只是一个 worker 槽位，不是预算。
        """

        async with self._lock:
            task = self._require(conversation_id, task_id)
        waiters: list[asyncio.Task[Any]] = [asyncio.ensure_future(task.process.wait())]
        if cancel_event is not None:
            waiters.append(asyncio.ensure_future(cancel_event.wait()))
        try:
            await asyncio.wait(waiters, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
        # 进程退出后，读取端还有没消费完的缓冲。不等一下 pump，最后一段输出（往往正是
        # 报错原因）就会掉在这次快照外面。
        if not task.running:
            for reader in task.readers:
                # shield：超时只放弃等待，不能把 pump 本身取消掉——后面还要靠它把
                # 剩余输出搬进缓冲区，供下一次 shell_task_output 读到。
                with suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(reader), timeout=1.0)
        async with self._lock:
            return task.snapshot(incremental=True)

    async def has_live_tasks(self, conversation_id: UUID) -> bool:
        async with self._lock:
            return any(
                task.conversation_id == conversation_id and task.running
                for task in self._tasks.values()
            )

    async def kill(self, *, conversation_id: UUID, task_id: str) -> ShellTaskSnapshot:
        async with self._lock:
            task = self._require(conversation_id, task_id)
        if task.running:
            await _terminate_process_group(task.process, self._terminate_grace_s)
        async with self._lock:
            return task.snapshot(incremental=False)

    async def aclose(self) -> None:
        async with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
        expiry_tasks = [task.expiry for task in tasks if task.expiry is not None]
        reader_tasks = [reader for task in tasks for reader in task.readers]
        for task in tasks:
            if task.expiry is not None:
                task.expiry.cancel()
            if task.running:
                await _terminate_process_group(task.process, self._terminate_grace_s)
            for reader in task.readers:
                reader.cancel()
        await asyncio.gather(*expiry_tasks, *reader_tasks, return_exceptions=True)

    def _require(self, conversation_id: UUID, task_id: str) -> _ShellTask:
        task = self._tasks.get(task_id)
        # 不区分"不存在"和"属于别的会话"：区分了就等于确认了别的会话里存在这个 id。
        if task is None or task.conversation_id != conversation_id:
            raise ShellTaskError(
                f"后台任务 {task_id} 不存在。它可能已经被清理，或者 worker 重启过——"
                "后台任务不跨重启存活，需要的话请重新启动这条命令"
            )
        return task

    async def _reap_locked(self) -> None:
        deadline = time.monotonic() - self._hard_ttl_s
        for task_id, task in list(self._tasks.items()):
            if task.started_at > deadline:
                continue
            # 超过绝对上限一律回收：没人来收的后台进程会一直占着这台机器。
            if task.running:
                await _terminate_process_group(task.process, self._terminate_grace_s)
            for reader in task.readers:
                reader.cancel()
            if task.expiry is not None and task.expiry is not asyncio.current_task():
                task.expiry.cancel()
            del self._tasks[task_id]

    def _prune_completed_locked(self, *, reserve: int) -> None:
        """为新任务留位置；只淘汰已结束快照，绝不拿保留上限杀活进程。"""

        overflow = len(self._tasks) + reserve - self._max_retained_tasks
        if overflow <= 0:
            return
        completed = sorted(
            (task for task in self._tasks.values() if not task.running),
            key=lambda task: (task.started_at, task.task_id),
        )
        for task in completed[:overflow]:
            self._tasks.pop(task.task_id, None)
            if task.expiry is not None:
                task.expiry.cancel()
            for reader in task.readers:
                reader.cancel()

    async def _expire(self, task: _ShellTask) -> None:
        try:
            delay = max(0.0, task.started_at + self._hard_ttl_s - time.monotonic())
            await asyncio.sleep(delay)
            async with self._lock:
                if self._tasks.get(task.task_id) is not task:
                    return
                del self._tasks[task.task_id]
            if task.running:
                await _terminate_process_group(task.process, self._terminate_grace_s)
            for reader in task.readers:
                reader.cancel()
        except asyncio.CancelledError:
            return

    async def _spawn(self, command: ShellCommand, cwd: Path) -> asyncio.subprocess.Process:
        environment = _minimal_environment()
        argv = ("/bin/sh", "-c", command.raw) if command.has_operators else command.argv
        return await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # 独立进程组：kill 时连同它派生的子进程一起收掉。
            start_new_session=os.name == "posix",
        )

    async def _pump(self, task: _ShellTask, stream: asyncio.StreamReader) -> None:
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return
            async with self._lock:
                task.absorb(chunk)
