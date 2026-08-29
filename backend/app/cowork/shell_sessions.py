"""会话级持久 PTY：进程活着时保留 cwd/env，重启后从最后 cwd 重生。

这里的「持久」有意拆成两种强度：

* PTY 子进程仍存活时，命令都在同一个 shell 里执行，所以 ``cd``、``export``、
  virtualenv 激活和 shell 函数会继续存在；
* WorkPilot 或 PTY 进程退出后，只把最后一次成功观测到的 cwd 写进 0600 JSON。
  下一次调用从该目录新建 shell，并明确返回 ``environment_status=lost_on_recovery``。

不序列化环境变量不是技术偷懒，而是安全边界：环境里常有临时 token、SSH agent socket
和工具注入的秘密。把它们整包落盘既扩大秘密面，也无法正确恢复指向旧进程的 socket。
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import select
import termios
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from app.core.private_json import read_private_json, write_private_json
from app.cowork.shell import (
    CoworkShellCancelledError,
    CoworkShellError,
    CoworkShellTimeoutError,
    ShellCommand,
    _minimal_environment,
    _terminate_process_group,
)

EnvironmentStatus = Literal["fresh", "preserved", "lost_on_recovery"]
_STATE_VERSION = 1
_PROTOCOL_WINDOW_BYTES = 32 * 1024


class ShellSessionError(CoworkShellError):
    """面向模型的持久 shell 错误。"""


@dataclass(frozen=True)
class PersistentShellResult:
    session_id: str
    command_sha256: str
    exit_code: int
    output: str
    output_truncated: bool
    cwd: str
    environment_status: EnvironmentStatus


@dataclass(frozen=True)
class _PersistedSession:
    cwd: Path


class _ShellSessionStateStore:
    """只保存可恢复的 cwd；永远不保存 env 或终端输出。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def get(self, conversation_id: UUID) -> _PersistedSession | None:
        with self._lock:
            raw = read_private_json(self.path).get("sessions")
        if not isinstance(raw, dict):
            return None
        value = raw.get(str(conversation_id))
        if not isinstance(value, dict):
            return None
        cwd = value.get("cwd")
        if not isinstance(cwd, str):
            return None
        path = Path(cwd).expanduser()
        if not path.is_absolute():
            return None
        return _PersistedSession(cwd=path)

    def put(self, conversation_id: UUID, *, cwd: Path) -> None:
        with self._lock:
            payload = read_private_json(self.path)
            raw = payload.get("sessions")
            sessions = dict(raw) if isinstance(raw, dict) else {}
            sessions[str(conversation_id)] = {
                "cwd": str(cwd),
                "updated_at": datetime.now(UTC).isoformat(),
            }
            write_private_json(
                self.path,
                {"version": _STATE_VERSION, "sessions": sessions},
            )


@dataclass
class _PtySession:
    conversation_id: UUID
    process: asyncio.subprocess.Process
    master_fd: int
    cwd: Path
    environment_status: EnvironmentStatus
    lock: asyncio.Lock


class CoworkPersistentShellManager:
    """每个 Cowork 会话一只串行 PTY shell。"""

    def __init__(
        self,
        *,
        state_path: Path,
        timeout_s: float,
        terminate_grace_s: float,
        max_output_bytes: int,
    ) -> None:
        self._store = _ShellSessionStateStore(state_path)
        self._timeout_s = timeout_s
        self._terminate_grace_s = terminate_grace_s
        self._max_output_bytes = max_output_bytes
        self._sessions: dict[UUID, _PtySession] = {}
        self._manager_lock = asyncio.Lock()

    async def execute(
        self,
        *,
        conversation_id: UUID,
        command: ShellCommand,
        cwd: Path,
        reset: bool = False,
        cancel_event: asyncio.Event | None = None,
    ) -> PersistentShellResult:
        if os.name != "posix":  # pragma: no cover - 当前桌面矩阵是 macOS/Linux
            raise ShellSessionError("持久 PTY shell 当前只支持 macOS/Linux")
        requested_cwd, requested_exists = await asyncio.to_thread(_resolve_directory, cwd)
        if not requested_exists:
            raise ShellSessionError(f"持久 shell cwd 不是现有目录：{requested_cwd}")
        session = await self._get_or_start(conversation_id, requested_cwd, reset=reset)
        async with session.lock:
            # PTY 可能在拿到 manager lock 之后、真正执行之前退出；此时仍按最后 cwd 重生。
            if session.process.returncode is not None:
                await self._discard(conversation_id, session)
                # 递归一次会为替换后的 PTY 获取它自己的串行锁；不能拿着旧 session 的
                # 锁直接操作新 PTY，否则另一条并发调用可能同时向新终端写入。
                return await self.execute(
                    conversation_id=conversation_id,
                    command=command,
                    cwd=requested_cwd,
                    reset=False,
                    cancel_event=cancel_event,
                )
            elif session.cwd != requested_cwd:
                # 同会话并发调用可能都在上一条命令完成前读到旧 cwd；拿到串行锁后必须
                # 再核一次，不能让后一条命令在已经变化的目录里悄悄执行。
                raise ShellSessionError(
                    f"持久 shell 当前 cwd 是 {session.cwd}，不是 {requested_cwd}。"
                    "使用上次结果返回的 cwd，或设置 reset_session=true 从新目录重建"
                )
            status = session.environment_status
            marker = uuid4().hex
            payload = _command_payload(command.raw, marker)
            try:
                await asyncio.to_thread(_write_all, session.master_fd, payload)
            except OSError as error:
                await self._discard(conversation_id, session)
                raise ShellSessionError(
                    "持久 PTY 已退出；下次调用会从最后 cwd 重建，但环境变量不会恢复"
                ) from error

            reader = asyncio.create_task(
                asyncio.to_thread(
                    _read_until_marker,
                    session.master_fd,
                    marker,
                    self._max_output_bytes,
                )
            )
            cancel = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
            waiters: set[asyncio.Task[object]] = {reader}
            if cancel is not None:
                waiters.add(cancel)
            done, _ = await asyncio.wait(
                waiters,
                timeout=self._timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel is not None:
                cancel.cancel()
            if reader not in done:
                await self._discard(conversation_id, session)
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                if cancel is not None and cancel in done:
                    raise CoworkShellCancelledError(
                        "用户停止，持久 PTY 已终止；下次从最后 cwd 重建且 env 丢失"
                    )
                raise CoworkShellTimeoutError(
                    f"持久 shell 命令超过 {self._timeout_s:g} 秒，PTY 已终止；"
                    "下次从最后 cwd 重建且 env 丢失"
                )
            try:
                exit_code, output, truncated, observed_cwd = reader.result()
            except Exception as error:
                await self._discard(conversation_id, session)
                raise ShellSessionError(
                    "持久 PTY 在命令完成前退出；下次调用会从最后 cwd 重建，"
                    "本次 shell 环境变量已经丢失"
                ) from error

            resolved_cwd, cwd_exists = await asyncio.to_thread(
                _resolve_directory, Path(observed_cwd)
            )
            if not cwd_exists:
                await self._discard(conversation_id, session)
                raise ShellSessionError(
                    "持久 PTY 返回的 cwd 已不存在；请用 reset_session=true 从有效目录重建"
                )
            session.cwd = resolved_cwd
            session.environment_status = "preserved"
            await asyncio.to_thread(
                self._store.put,
                conversation_id,
                cwd=resolved_cwd,
            )
            return PersistentShellResult(
                session_id=f"pty:{conversation_id}",
                command_sha256=hashlib.sha256(command.raw.encode("utf-8")).hexdigest(),
                exit_code=exit_code,
                output=output,
                output_truncated=truncated,
                cwd=str(resolved_cwd),
                environment_status=status,
            )

    async def aclose(self) -> None:
        async with self._manager_lock:
            sessions = list(self._sessions.items())
            self._sessions.clear()
        for _, session in sessions:
            await self._close_process(session)

    async def current_cwd(self, conversation_id: UUID) -> Path | None:
        """返回会话当前/最后 cwd，供省略 ``cwd`` 的工具调用安全复用。"""

        async with self._manager_lock:
            session = self._sessions.get(conversation_id)
            if session is not None and session.process.returncode is None:
                return session.cwd
        persisted = await asyncio.to_thread(self._store.get, conversation_id)
        return None if persisted is None else persisted.cwd

    async def _get_or_start(
        self, conversation_id: UUID, requested_cwd: Path, *, reset: bool
    ) -> _PtySession:
        async with self._manager_lock:
            existing = self._sessions.get(conversation_id)
            if existing is not None and (reset or existing.process.returncode is not None):
                del self._sessions[conversation_id]
                await self._close_process(existing)
                existing = None
            if existing is not None and existing.process.returncode is None:
                if existing.cwd != requested_cwd:
                    raise ShellSessionError(
                        f"持久 shell 当前 cwd 是 {existing.cwd}，不是 {requested_cwd}。"
                        "使用上次结果返回的 cwd，或设置 reset_session=true 从新目录重建"
                    )
                return existing

            persisted = await asyncio.to_thread(self._store.get, conversation_id)
            if persisted is not None and not reset:
                recovered = True
                start_cwd, cwd_exists = await asyncio.to_thread(_resolve_directory, persisted.cwd)
            else:
                recovered = False
                start_cwd, cwd_exists = requested_cwd, True
            if start_cwd != requested_cwd:
                raise ShellSessionError(
                    f"持久 shell 上次 cwd 是 {start_cwd}，不是 {requested_cwd}。"
                    "使用上次 cwd 恢复，或设置 reset_session=true 放弃旧环境"
                )
            if not cwd_exists:
                raise ShellSessionError(
                    f"持久 shell 的 cwd {start_cwd} 已不存在；"
                    "请指定有效目录并设置 reset_session=true"
                )
            session = await self._spawn(
                conversation_id,
                start_cwd,
                environment_status="lost_on_recovery" if recovered else "fresh",
            )
            self._sessions[conversation_id] = session
            return session

    async def _spawn(
        self,
        conversation_id: UUID,
        cwd: Path,
        *,
        environment_status: EnvironmentStatus,
    ) -> _PtySession:
        master_fd, slave_fd = os.openpty()
        try:
            attributes = termios.tcgetattr(slave_fd)
            attributes[3] &= ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(slave_fd, termios.TCSANOW, attributes)
            environment = _minimal_environment()
            environment.update(
                {
                    "PS1": "",
                    "PS2": "",
                    # POSIX shell 启动文件可能执行任意用户逻辑；持久性只来自这只 PTY，
                    # 不靠隐式 source 用户配置。
                    "ENV": "/dev/null",
                    "BASH_ENV": "/dev/null",
                }
            )
            process = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-i",
                cwd=str(cwd),
                env=environment,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
            )
        except BaseException:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        return _PtySession(
            conversation_id=conversation_id,
            process=process,
            master_fd=master_fd,
            cwd=cwd,
            environment_status=environment_status,
            lock=asyncio.Lock(),
        )

    async def _discard(self, conversation_id: UUID, session: _PtySession) -> None:
        async with self._manager_lock:
            if self._sessions.get(conversation_id) is session:
                del self._sessions[conversation_id]
        await self._close_process(session)

    async def _close_process(self, session: _PtySession) -> None:
        if session.process.returncode is None:
            await _terminate_process_group(session.process, self._terminate_grace_s)
        try:
            os.close(session.master_fd)
        except OSError:
            pass


def _command_payload(command: str, marker: str) -> bytes:
    # RS/US 控制字符把协议帧与普通终端文本分开。cwd 放在两只 marker 之间，路径里即使
    # 有空格或换行也不会破坏解析；只有极端路径含相同随机 marker 才可能冲突。
    status_var = f"__workpilot_status_{marker}"
    cwd_var = f"__workpilot_cwd_{marker}"
    trailer = (
        f"\n{status_var}=$?\n"
        f"{cwd_var}=$(command pwd -P 2>/dev/null || command printf '%s' \"$PWD\")\n"
        f"command printf '\\036{marker}:%s\\037%s\\036{marker}:end\\037\\n' "
        f'"${status_var}" "${cwd_var}"\n'
    )
    return f"{command}{trailer}".encode()


def _resolve_directory(path: Path) -> tuple[Path, bool]:
    resolved = path.expanduser().resolve()
    return resolved, resolved.is_dir()


def _write_all(fd: int, payload: bytes) -> None:
    """PTY 也可能发生短写；协议命令必须完整送达，不能假设一次 write 就够。"""

    remaining = memoryview(payload)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except InterruptedError:
            continue
        if written <= 0:
            raise ShellSessionError("PTY 未接受命令输入")
        remaining = remaining[written:]


def _read_until_marker(fd: int, marker: str, max_output_bytes: int) -> tuple[int, str, bool, str]:
    prefix = f"\x1e{marker}:".encode()
    suffix = f"\x1e{marker}:end\x1f".encode()
    retained = bytearray()
    pending = bytearray()
    total_output = 0

    def retain(chunk: bytes) -> None:
        nonlocal total_output
        total_output += len(chunk)
        retained.extend(chunk)
        if len(retained) > max_output_bytes:
            del retained[: len(retained) - max_output_bytes]

    while True:
        readable, _, _ = select.select([fd], [], [], 0.25)
        if not readable:
            continue
        try:
            chunk = os.read(fd, 8192)
        except OSError as error:
            if error.errno == errno.EIO:
                raise ShellSessionError("PTY 已关闭，未收到命令完成标记") from error
            raise
        if not chunk:
            raise ShellSessionError("PTY 已关闭，未收到命令完成标记")
        pending.extend(chunk)
        start = pending.find(prefix)
        if start < 0:
            safe = max(0, len(pending) - len(prefix) + 1)
            if safe:
                retain(bytes(pending[:safe]))
                del pending[:safe]
            continue
        retain(bytes(pending[:start]))
        protocol = pending[start + len(prefix) :]
        while suffix not in protocol:
            if len(protocol) > _PROTOCOL_WINDOW_BYTES:
                raise ShellSessionError("PTY 完成标记里的 cwd 超过协议上限")
            readable, _, _ = select.select([fd], [], [], 0.25)
            if not readable:
                continue
            try:
                more = os.read(fd, 8192)
            except OSError as error:
                raise ShellSessionError("PTY 在完成标记中途关闭") from error
            if not more:
                raise ShellSessionError("PTY 在完成标记中途关闭")
            protocol.extend(more)
        end = protocol.index(suffix)
        frame = bytes(protocol[:end])
        separator = frame.find(b"\x1f")
        if separator < 1:
            raise ShellSessionError("PTY 返回了无效的完成标记")
        try:
            exit_code = int(frame[:separator].decode("ascii"))
        except ValueError as error:
            raise ShellSessionError("PTY 返回了无效的退出码") from error
        cwd = frame[separator + 1 :].decode("utf-8", errors="strict")
        output = bytes(retained).replace(b"\r\n", b"\n").decode("utf-8", errors="replace")
        return exit_code, output, total_output > max_output_bytes, cwd


__all__ = [
    "CoworkPersistentShellManager",
    "PersistentShellResult",
    "ShellSessionError",
]
