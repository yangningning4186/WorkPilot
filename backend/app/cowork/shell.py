"""Cowork 受控 shell 策略与可终止子进程执行器。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from app.cowork.self_protection import shell_argument_indirection_reason

SHELL_OPERATOR_TOKENS = ("\n", "\r", ";", "|", "&", ">", "<", "`", "$")

# 前缀规则只能为当前 argv 背书，不能为参数里藏着的第二个程序或源码背书。
_ARG_EXECUTORS = frozenset(
    {
        "xargs",
        "env",
        "nohup",
        "nice",
        "stdbuf",
        "timeout",
        "watch",
        "sudo",
        "doas",
        "ssh",
        "docker",
        "podman",
        "kubectl",
        "npx",
        "pnpx",
        "bunx",
        "uvx",
    }
)
_INTERPRETERS = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "dash",
        "ksh",
        "fish",
        "powershell",
        "pwsh",
        "cmd",
        "python",
        "python3",
        "node",
        "deno",
        "bun",
        "ruby",
        "perl",
        "php",
    }
)
_INLINE_CODE_FLAGS = frozenset(
    {"-c", "-e", "--eval", "--command", "-command", "-encodedcommand", "/c", "/k"}
)
_DANGEROUS_FLAGS = frozenset({"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprintf"})


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
    prefix_ineligible_reason: str | None = None

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
    full_output_path: str | None = None
    full_output_truncated: bool = False
    full_output_size_bytes: int = 0


@dataclass
class _OutputCapture:
    handle: BinaryIO
    max_bytes: int
    lock: asyncio.Lock
    written: int = 0
    truncated: bool = False

    async def append(self, channel: str, chunk: bytes) -> None:
        marker = f"\n--- {channel} ---\n".encode()
        async with self.lock:
            payload = marker + chunk
            room = self.max_bytes - self.written
            if room > 0:
                written = self.handle.write(payload[:room])
                self.written += written
            if len(payload) > room:
                self.truncated = True


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
        ineligible = prefix_ineligibility_reason(parsed.argv)
        if ineligible is not None:
            raise CoworkShellError(f"shell allowlist 条目不能自动放行：{ineligible}")
        prefixes.append(parsed.argv)
    return tuple(prefixes)


def assess_shell_command(command: str, allowlist: list[str]) -> ShellDecision:
    parsed = parse_shell_command(command)
    prefixes = compile_allowlist(allowlist)
    ineligible = prefix_ineligibility_reason(parsed.argv)
    if parsed.has_operators:
        return ShellDecision(
            command=parsed,
            allowlisted=False,
            matched_prefix=None,
            prefix_ineligible_reason="包含 shell 操作符或不透明展开",
        )
    if ineligible is not None:
        return ShellDecision(
            command=parsed,
            allowlisted=False,
            matched_prefix=None,
            prefix_ineligible_reason=ineligible,
        )
    matched = next(
        (
            prefix
            for prefix in prefixes
            if len(parsed.argv) >= len(prefix) and parsed.argv[: len(prefix)] == prefix
        ),
        None,
    )
    return ShellDecision(
        command=parsed,
        allowlisted=matched is not None,
        matched_prefix=matched,
    )


def prefix_ineligibility_reason(argv: tuple[str, ...] | list[str]) -> str | None:
    """说明为什么一条 argv 不能被 prefix allowlist 自动授权。"""

    if not argv:
        return "命令没有可校验的 argv"
    program = Path(argv[0]).name.casefold()
    if program.endswith(".exe"):
        program = program[:-4]
    if program in _ARG_EXECUTORS:
        return f"{program} 会执行参数中指定的另一个程序"
    lowered = tuple(argument.casefold() for argument in argv[1:])
    if _is_interpreter(program) and any(_is_inline_code_flag(argument) for argument in lowered):
        return f"{program} 携带内联代码参数"
    dangerous = next((argument for argument in lowered if argument in _DANGEROUS_FLAGS), None)
    if dangerous is not None:
        return f"参数 {dangerous} 会执行程序或删除文件"
    if program in {"npm", "pnpm", "yarn"} and lowered[:1] in {
        ("exec",),
        ("x",),
        ("dlx",),
    }:
        return f"{program} {lowered[0]} 会执行参数中指定的程序"
    return shell_argument_indirection_reason(argv)


def _is_interpreter(program: str) -> bool:
    if program in _INTERPRETERS:
        return True
    if not program.startswith("python"):
        return False
    suffix = program.removeprefix("python")
    return bool(suffix) and all(part.isdigit() for part in suffix.split("."))


def _is_inline_code_flag(argument: str) -> bool:
    if argument in _INLINE_CODE_FLAGS:
        return True
    return (
        (argument.startswith("-c") or argument.startswith("-e"))
        and len(argument) > 2
        and not argument.startswith("--")
    ) or any(
        argument.startswith(f"{flag}=")
        for flag in ("--eval", "--command", "-command", "-encodedcommand")
    )


async def execute_shell_command(
    command: ShellCommand,
    *,
    cwd: Path,
    cancel_event: asyncio.Event | None,
    timeout_s: float,
    terminate_grace_s: float,
    max_output_bytes: int,
    full_output_path: Path | None = None,
    full_output_max_bytes: int = 64 * 1024 * 1024,
) -> ShellExecutionResult:
    if timeout_s <= 0 or terminate_grace_s < 0 or max_output_bytes < 1 or full_output_max_bytes < 1:
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
    capture: _OutputCapture | None = None
    if full_output_path is not None:
        full_output_path.parent.mkdir(parents=True, exist_ok=True)
        capture = _OutputCapture(
            handle=full_output_path.open("wb"),
            max_bytes=full_output_max_bytes,
            lock=asyncio.Lock(),
        )
    stdout_task = asyncio.create_task(
        _read_limited(process.stdout, max_output_bytes, capture=capture, channel="stdout")
    )
    stderr_task = asyncio.create_task(
        _read_limited(process.stderr, max_output_bytes, capture=capture, channel="stderr")
    )
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
        (stdout_bytes, stdout_truncated), (stderr_bytes, stderr_truncated) = await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task),
            timeout=max(terminate_grace_s, 0.5),
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
    finally:
        if capture is not None:
            capture.handle.flush()
            capture.handle.close()
    if not process_task.done():
        # asyncio 的 subprocess wait transport 在 returncode 已产生后仍可能等待继承的
        # stdout/stderr 管道 EOF；reader 已超时就不能再在这里无界等待。
        process_task.cancel()
    await asyncio.gather(process_task, return_exceptions=True)
    if pending_error is not None:
        raise pending_error
    if reader_timed_out:
        raise CoworkShellError("shell 主进程退出后输出管道未关闭，已停止读取")
    output_truncated = stdout_truncated or stderr_truncated
    retained_output_path: str | None = None
    if full_output_path is not None:
        if output_truncated:
            retained_output_path = str(full_output_path)
        else:
            # This file was created by this invocation solely as a fallback.  Short output is
            # already complete in the result and should not clutter the user's workspace.
            await asyncio.to_thread(full_output_path.unlink, missing_ok=True)
    return ShellExecutionResult(
        command_sha256=hashlib.sha256(command.raw.encode("utf-8")).hexdigest(),
        exit_code=exit_code,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        output_truncated=output_truncated,
        execution_mode=execution_mode,
        full_output_path=retained_output_path,
        full_output_truncated=False if capture is None else capture.truncated,
        full_output_size_bytes=0 if capture is None else capture.written,
    )


async def _read_limited(
    stream: asyncio.StreamReader,
    max_bytes: int,
    *,
    capture: _OutputCapture | None = None,
    channel: str = "output",
) -> tuple[bytes, bool]:
    retained = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return bytes(retained), truncated
        if capture is not None:
            await capture.append(channel, chunk)
        retained.extend(chunk)
        if len(retained) > max_bytes:
            del retained[: len(retained) - max_bytes]
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
    except PermissionError:
        # A POSIX sandbox may allow signalling the direct child but reject killpg even though
        # WorkPilot created the group.  Fall back to the owned child; pipe transports are closed
        # separately by the caller, so a detached grandchild cannot keep this request hanging.
        try:
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
    except PermissionError:
        try:
            process.kill()
        except ProcessLookupError:
            return
    await process.wait()


def _minimal_environment() -> dict[str, str]:
    # 开发版 sidecar 直接由 backend/.venv/bin/python 启动。把同一解释器目录放到 PATH
    # 最前，格式 Skill 调用 python/python3 时才能稳定拿到随 WorkPilot 安装的
    # python-docx/openpyxl/python-pptx/PyMuPDF，而不是随机落到系统 Python。
    executable_dir = str(Path(sys.executable).resolve().parent)
    inherited_path = os.environ.get("PATH", os.defpath)
    environment = {
        "PATH": os.pathsep.join((executable_dir, inherited_path)),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }
    if Path(sys.executable).name.casefold().startswith("python"):
        environment["WORKPILOT_PYTHON"] = str(Path(sys.executable).resolve())
    for key in ("HOME", "TMPDIR"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment
