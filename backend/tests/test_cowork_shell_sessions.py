"""会话级持久 PTY：活进程保留环境，重启只恢复 cwd。"""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.cowork.shell import parse_shell_command
from app.cowork.shell_sessions import CoworkPersistentShellManager, ShellSessionError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="PTY 只在 POSIX 桌面启用")


def _manager(state_path: Path) -> CoworkPersistentShellManager:
    return CoworkPersistentShellManager(
        state_path=state_path,
        timeout_s=5,
        terminate_grace_s=0.2,
        max_output_bytes=4096,
    )


async def test_persistent_pty_keeps_cwd_and_environment_between_calls(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "state.json")
    conversation_id = uuid4()
    child = tmp_path / "child"
    child.mkdir()
    try:
        first = await manager.execute(
            conversation_id=conversation_id,
            command=parse_shell_command("export WORKPILOT_PTY_VALUE=alive; cd child"),
            cwd=tmp_path,
        )
        assert first.environment_status == "fresh"
        assert first.cwd == str(child.resolve())
        assert await manager.current_cwd(conversation_id) == child.resolve()

        second = await manager.execute(
            conversation_id=conversation_id,
            command=parse_shell_command('printf \'%s|%s\' "$WORKPILOT_PTY_VALUE" "$PWD"'),
            cwd=child,
        )
        assert second.environment_status == "preserved"
        assert f"alive|{child.resolve()}" in second.output
    finally:
        await manager.aclose()


async def test_restart_recovers_last_cwd_but_explicitly_loses_environment(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    conversation_id = uuid4()
    child = tmp_path / "child"
    child.mkdir()
    first_manager = _manager(state_path)
    await first_manager.execute(
        conversation_id=conversation_id,
        command=parse_shell_command("export WORKPILOT_PTY_VALUE=secret; cd child"),
        cwd=tmp_path,
    )
    await first_manager.aclose()

    recovered_manager = _manager(state_path)
    try:
        assert await recovered_manager.current_cwd(conversation_id) == child.resolve()
        recovered = await recovered_manager.execute(
            conversation_id=conversation_id,
            command=parse_shell_command('printf \'%s|%s\' "${WORKPILOT_PTY_VALUE-unset}" "$PWD"'),
            cwd=child,
        )
        assert recovered.environment_status == "lost_on_recovery"
        assert recovered.cwd == str(child.resolve())
        assert f"unset|{child.resolve()}" in recovered.output
    finally:
        await recovered_manager.aclose()


async def test_persistent_session_refuses_silent_cwd_drift(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "state.json")
    conversation_id = uuid4()
    other = tmp_path / "other"
    other.mkdir()
    try:
        await manager.execute(
            conversation_id=conversation_id,
            command=parse_shell_command("true"),
            cwd=tmp_path,
        )
        with pytest.raises(ShellSessionError, match="当前 cwd"):
            await manager.execute(
                conversation_id=conversation_id,
                command=parse_shell_command("pwd"),
                cwd=other,
            )
    finally:
        await manager.aclose()


async def test_reset_session_discards_environment_and_moves_cwd(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "state.json")
    conversation_id = uuid4()
    other = tmp_path / "other"
    other.mkdir()
    try:
        await manager.execute(
            conversation_id=conversation_id,
            command=parse_shell_command("export WORKPILOT_PTY_VALUE=alive"),
            cwd=tmp_path,
        )
        reset = await manager.execute(
            conversation_id=conversation_id,
            command=parse_shell_command("printf '%s' \"${WORKPILOT_PTY_VALUE-unset}\""),
            cwd=other,
            reset=True,
        )
        assert reset.environment_status == "fresh"
        assert "unset" in reset.output
        assert reset.cwd == str(other.resolve())
    finally:
        await manager.aclose()


async def test_concurrent_call_rechecks_cwd_after_serialization(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "state.json")
    conversation_id = uuid4()
    child = tmp_path / "child"
    child.mkdir()
    try:
        first = asyncio.create_task(
            manager.execute(
                conversation_id=conversation_id,
                command=parse_shell_command("sleep 0.1; cd child"),
                cwd=tmp_path,
            )
        )
        await asyncio.sleep(0.02)
        second = asyncio.create_task(
            manager.execute(
                conversation_id=conversation_id,
                command=parse_shell_command("pwd"),
                cwd=tmp_path,
            )
        )

        assert (await first).cwd == str(child.resolve())
        with pytest.raises(ShellSessionError, match="当前 cwd"):
            await second
    finally:
        await manager.aclose()


async def test_protocol_ignores_user_printf_function_and_internal_name_collision(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path / "state.json")
    conversation_id = uuid4()
    try:
        result = await manager.execute(
            conversation_id=conversation_id,
            command=parse_shell_command(
                "printf() { :; }; __workpilot_status=poison; __workpilot_cwd=poison"
            ),
            cwd=tmp_path,
        )

        assert result.exit_code == 0
        assert result.cwd == str(tmp_path)
    finally:
        await manager.aclose()
