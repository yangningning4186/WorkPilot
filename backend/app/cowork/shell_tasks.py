"""后台 shell 任务：跑得比一次工具调用久的命令。

`run_shell` 是同步的：起进程、等它结束、把输出交回去。dev server、长构建、watch 进程
在这个模型下要么撞超时，要么把整个 run 卡住。后台任务把"启动"和"取输出"拆成两步，
模型可以先去做别的事，再回来轮询。

**进程活在 worker 进程里，不落库。** 这不是偷懒：真正的持久 shell（保留 cd、venv、
环境变量的交互式会话）在我们的架构里给不出正确语义——run 会在 `waiting_human` 处暂停，
恢复时可能落到另一个 worker，那边的 PTY 里 cwd 是错的，而命令照样会跑，错得悄无声息。
后台任务同样活不过重启，但失败是**显式**的：`shell_task_output` 会直截了当地说任务不在了。
两者的区别就是这一点，也是这里只做后台任务、不做持久 shell 的理由。

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
    ) -> None:
        self._tasks: dict[str, _ShellTask] = {}
        self._lock = asyncio.Lock()
        self._max_tasks = max_tasks_per_conversation
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
            await asyncio.wait(
                waiters, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED
            )
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
        for task in tasks:
            if task.running:
                await _terminate_process_group(task.process, self._terminate_grace_s)
            for reader in task.readers:
                reader.cancel()

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
            del self._tasks[task_id]

    async def _spawn(
        self, command: ShellCommand, cwd: Path
    ) -> asyncio.subprocess.Process:
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
