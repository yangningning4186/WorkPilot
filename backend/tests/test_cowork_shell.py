import asyncio
import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from app.cowork.sandbox import CoworkSandboxError, SandboxLimits, build_sandbox_argv
from app.cowork.shell import (
    CoworkShellCancelledError,
    CoworkShellError,
    assess_shell_command,
    compile_allowlist,
    execute_shell_command,
    parse_shell_command,
)


def test_shell_allowlist_uses_exact_argv_prefix_and_operators_never_match() -> None:
    allowed = ["git status", "python -m pytest"]

    status = assess_shell_command("git status --short", allowed)
    assert status.allowlisted is True
    assert status.matched_prefix == ("git", "status")
    assert assess_shell_command("git statusx", allowed).allowlisted is False
    chained = assess_shell_command("git status && rm -rf /tmp/not-run", allowed)
    assert chained.command.has_operators is True
    assert chained.allowlisted is False

    with pytest.raises(CoworkShellError, match="不能包含操作符"):
        compile_allowlist(["git status && echo unsafe"])


def test_sandbox_backend_has_hard_isolation_flags_and_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.cowork.sandbox.shutil.which", lambda name: f"/usr/bin/{name}")
    limits = SandboxLimits(
        runtime="docker",
        image="alpine:3.20",
        memory_mb=256,
        pids_limit=64,
        cpus=0.5,
    )
    argv = build_sandbox_argv(
        command="printf ok; touch output.txt",
        cwd=tmp_path,
        limits=limits,
    )

    assert argv[0] == "/usr/bin/docker"
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert f"type=bind,source={tmp_path.resolve()},target=/workspace" in argv
    assert argv[-3:] == ("/bin/sh", "-lc", "printf ok; touch output.txt")

    with pytest.raises(CoworkSandboxError, match="不能降级"):
        build_sandbox_argv(
            command="true",
            cwd=tmp_path,
            limits=SandboxLimits(
                runtime="disabled",
                image="alpine:3.20",
                memory_mb=256,
                pids_limit=64,
                cpus=0.5,
            ),
        )


async def test_shell_execution_caps_output_and_does_not_inherit_secrets(tmp_path: Path) -> None:
    os.environ["WORKPILOT_TEST_SECRET"] = "must-not-reach-child"
    try:
        command = parse_shell_command(
            f"{shlex.quote(sys.executable)} -c "
            + shlex.quote("import os; print(os.getenv('WORKPILOT_TEST_SECRET')); print('x'*5000)")
        )
        result = await execute_shell_command(
            command,
            cwd=tmp_path,
            cancel_event=None,
            timeout_s=5,
            terminate_grace_s=0.2,
            max_output_bytes=512,
        )
    finally:
        os.environ.pop("WORKPILOT_TEST_SECRET", None)

    assert result.exit_code == 0
    assert result.output_truncated is True
    assert len(result.stdout.encode()) == 512
    assert "must-not-reach-child" not in result.stdout


async def test_operator_shell_does_not_source_user_login_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "profile-was-sourced"
    (tmp_path / ".profile").write_text(f"touch {shlex.quote(str(marker))}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    result = await execute_shell_command(
        parse_shell_command("printf profile-safe | cat"),
        cwd=tmp_path,
        cancel_event=None,
        timeout_s=5,
        terminate_grace_s=0.2,
        max_output_bytes=1024,
    )

    assert result.stdout == "profile-safe"
    assert not marker.exists()


async def test_shell_cancel_terminates_operator_process_group_promptly(tmp_path: Path) -> None:
    cancel_event = asyncio.Event()
    command = parse_shell_command("sleep 30 | cat")
    started = time.monotonic()
    task = asyncio.create_task(
        execute_shell_command(
            command,
            cwd=tmp_path,
            cancel_event=cancel_event,
            timeout_s=60,
            terminate_grace_s=0.2,
            max_output_bytes=1024,
        )
    )
    await asyncio.sleep(0.1)
    cancel_event.set()

    with pytest.raises(CoworkShellCancelledError, match="用户停止"):
        await task
    assert time.monotonic() - started < 2


@pytest.mark.skipif(os.name != "posix", reason="setsid process-group behavior is POSIX-only")
async def test_shell_does_not_hang_when_detached_child_keeps_output_pipe(tmp_path: Path) -> None:
    pid_path = tmp_path / "detached.pid"
    child_script = "import time; time.sleep(30)"
    parent_script = (
        "import subprocess,sys; from pathlib import Path; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_script!r}], start_new_session=True); "
        f"Path({str(pid_path)!r}).write_text(str(p.pid))"
    )
    cancel_event = asyncio.Event()
    started = time.monotonic()
    try:
        task = asyncio.create_task(
            execute_shell_command(
                parse_shell_command(
                    f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_script)}"
                ),
                cwd=tmp_path,
                cancel_event=cancel_event,
                timeout_s=60,
                terminate_grace_s=0.05,
                max_output_bytes=1024,
            )
        )
        for _ in range(20):
            if pid_path.exists():
                break
            await asyncio.sleep(0.01)
        cancel_event.set()
        with pytest.raises(CoworkShellCancelledError, match="用户停止"):
            await task
    finally:
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text()), 9)
            except ProcessLookupError:
                pass

    assert time.monotonic() - started < 2
