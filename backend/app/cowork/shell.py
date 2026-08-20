"""Cowork 受控 shell 策略与可终止子进程执行器。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SHELL_OPERATOR_TOKENS = ("\n", "\r", ";", "|", "&", ">", "<", "`", "$(")


class CoworkShellError(RuntimeError):
    pass


class CoworkShellCancelledError(CoworkShellError):
    pass


class CoworkShellTimeoutError(CoworkShellError):
    pass


@dataclass(frozen=True)
class ShellCommand:
    raw: str
    argv: tuple[str, ...]
    has_operators: bool


@dataclass(frozen=True)
class ShellDecision:
    command: ShellCommand
    allowlisted: bool
    matched_prefix: tuple[str, ...] | None

    @property
    def approval_required(self) -> bool:
        return not self.allowlisted


@dataclass(frozen=True)
class ShellExecutionResult:
    command_sha256: str
    exit_code: int
    stdout: str
    stderr: str
    output_truncated: bool
    execution_mode: Literal["argv", "shell"]


def parse_shell_command(command: str) -> ShellCommand:
    normalized = command.strip()
    if not normalized:
        raise CoworkShellError("shell 命令不能为空")
    if len(normalized) > 4000:
        raise CoworkShellError("shell 命令不能超过 4000 个字符")
    if "\x00" in normalized:
        raise CoworkShellError("shell 命令包含非法空字符")
    has_operators = any(token in normalized for token in SHELL_OPERATOR_TOKENS)
    try:
        argv = tuple(shlex.split(normalized, posix=True))
    except ValueError as error:
        raise CoworkShellError(f"shell 命令无法解析：{error}") from error
    if not argv:
        raise CoworkShellError("shell 命令不能为空")
    if len(argv) > 256 or any(len(argument) > 4096 for argument in argv):
        raise CoworkShellError("shell 命令参数过多或单个参数过长")
    return ShellCommand(raw=normalized, argv=argv, has_operators=has_operators)


def compile_allowlist(entries: list[str]) -> tuple[tuple[str, ...], ...]:
    prefixes: list[tuple[str, ...]] = []
    for entry in entries:
        parsed = parse_shell_command(entry)
        if parsed.has_operators:
            raise CoworkShellError("shell allowlist 条目不能包含操作符")
        prefixes.append(parsed.argv)
    return tuple(prefixes)


def assess_shell_command(command: str, allowlist: list[str]) -> ShellDecision:
    parsed = parse_shell_command(command)
    prefixes = compile_allowlist(allowlist)
    if parsed.has_operators:
        return ShellDecision(command=parsed, allowlisted=False, matched_prefix=None)
    matched = next(
        (
            prefix
            for prefix in prefixes
            if len(parsed.argv) >= len(prefix) and parsed.argv[: len(prefix)] == prefix
        ),
        None,
    )
    return ShellDecision(command=parsed, allowlisted=matched is not None, matched_prefix=matched)


async def execute_shell_command(
    command: ShellCommand,
    *,
    cwd: Path,
    cancel_event: asyncio.Event | None,
    timeout_s: float,
    terminate_grace_s: float,
    max_output_bytes: int,
) -> ShellExecutionResult:
    if timeout_s <= 0 or terminate_grace_s < 0 or max_output_bytes < 1:
        raise ValueError("shell 执行限制必须为正数")
    environment = _minimal_environment()
    if command.has_operators:
        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            command.raw,
            cwd=str(cwd),
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        execution_mode: Literal["argv", "shell"] = "shell"
    else:
        process = await asyncio.create_subprocess_exec(
            *command.argv,
            cwd=str(cwd),
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        execution_mode = "argv"

    assert process.stdout is not None and process.stderr is not None
    stdout_task = asyncio.create_task(_read_limited(process.stdout, max_output_bytes))
    stderr_task = asyncio.create_task(_read_limited(process.stderr, max_output_bytes))
    process_task = asyncio.create_task(process.wait())
    cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
    waiters: set[asyncio.Task[int] | asyncio.Task[bool]] = {process_task}
    if cancel_task is not None:
        waiters.add(cancel_task)
    pending_error: CoworkShellError | None = None
    exit_code = -1
    try:
        done, _ = await asyncio.wait(
            waiters,
            timeout=timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if process_task in done:
            exit_code = process_task.result()
        elif cancel_task is not None and cancel_task in done:
            await _terminate_process_group(process, terminate_grace_s)
            pending_error = CoworkShellCancelledError("用户停止，shell 进程已终止")
        else:
            await _terminate_process_group(process, terminate_grace_s)
            pending_error = CoworkShellTimeoutError(f"shell 命令超过 {timeout_s:g} 秒，已终止")
    finally:
        if cancel_task is not None:
            cancel_task.cancel()
        if process.returncode is None:
            await _terminate_process_group(process, terminate_grace_s)

    reader_timed_out = False
    try:
        (stdout_bytes, stdout_truncated), (stderr_bytes, stderr_truncated) = (
            await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task),
                timeout=max(terminate_grace_s, 0.5),
            )
        )
    except TimeoutError:
        # 已脱离进程组的后代仍可能继承 stdout/stderr 管道。主进程即使退出，EOF
        # 也不会到达；reader 必须有独立上限，不能让取消/超时路径永久挂起。
        reader_timed_out = True
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        stdout_bytes, stdout_truncated = b"", True
        stderr_bytes, stderr_truncated = b"", True
    if not process_task.done():
        # asyncio 的 subprocess wait transport 在 returncode 已产生后仍可能等待继承的
        # stdout/stderr 管道 EOF；reader 已超时就不能再在这里无界等待。
        process_task.cancel()
    await asyncio.gather(process_task, return_exceptions=True)
    if pending_error is not None:
        raise pending_error
    if reader_timed_out:
        raise CoworkShellError("shell 主进程退出后输出管道未关闭，已停止读取")
    return ShellExecutionResult(
        command_sha256=hashlib.sha256(command.raw.encode("utf-8")).hexdigest(),
        exit_code=exit_code,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        output_truncated=stdout_truncated or stderr_truncated,
        execution_mode=execution_mode,
    )


async def _read_limited(stream: asyncio.StreamReader, max_bytes: int) -> tuple[bytes, bool]:
    retained = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return bytes(retained), truncated
        remaining = max_bytes - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True


async def _terminate_process_group(process: asyncio.subprocess.Process, grace_s: float) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - Windows CI 不在当前矩阵
            process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_s)
        return
    except TimeoutError:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover
            process.kill()
    except ProcessLookupError:
        return
    await process.wait()


def _minimal_environment() -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }
    for key in ("HOME", "TMPDIR"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment
