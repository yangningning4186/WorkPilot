"""后台 shell 任务。

同步 run_shell 跑不了 dev server、长构建和 watch 进程：要么撞超时，要么把整个 run 卡住。
这里验证的是"启动/轮询/结束"三步的语义，以及三条硬边界——会话隔离、并发上限、进程组回收。
"""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from app.cowork.shell import assess_shell_command
from app.cowork.shell_tasks import CoworkShellTaskManager, ShellTaskError

pytestmark = pytest.mark.integration


def _command(raw: str):
    return assess_shell_command(raw, []).command


def _manager(**overrides) -> CoworkShellTaskManager:
    defaults = {
        "max_tasks_per_conversation": 2,
        "output_max_bytes": 4096,
        "hard_ttl_s": 60.0,
        "terminate_grace_s": 0.5,
    }
    return CoworkShellTaskManager(**{**defaults, **overrides})


async def _wait_until(predicate, budget_s: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + budget_s
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("等待超时")


async def test_polling_returns_only_new_output(tmp_path: Path) -> None:
    """轮询回增量而不是全量：每次都把整段历史再塞一遍会撑爆上下文。"""

    manager = _manager()
    conversation_id = uuid4()
    try:
        started = await manager.start(
            conversation_id=conversation_id,
            command=_command("sh -c 'echo one; sleep 0.3; echo two'"),
            cwd=tmp_path,
        )
        assert started.running is True and started.exit_code is None

        await _wait_until(lambda: _has_text(manager, conversation_id, started.task_id, "one"))
        first = await manager.read(conversation_id=conversation_id, task_id=started.task_id)
        assert "one" in first.output

        await _wait_until(lambda: _finished(manager, conversation_id, started.task_id))
        second = await manager.read(conversation_id=conversation_id, task_id=started.task_id)
        # 增量：第二次读不该再带上第一次已经交付过的内容。
        assert "two" in second.output
        assert "one" not in second.output
        assert second.running is False and second.exit_code == 0

        # full=true 才回全量。
        whole = await manager.read(
            conversation_id=conversation_id, task_id=started.task_id, full=True
        )
        assert "one" in whole.output and "two" in whole.output
    finally:
        await manager.aclose()


async def _has_text(manager, conversation_id, task_id, text: str) -> bool:
    snapshot = await manager.read(conversation_id=conversation_id, task_id=task_id, full=True)
    return text in snapshot.output


async def _finished(manager, conversation_id, task_id) -> bool:
    snapshot = await manager.read(conversation_id=conversation_id, task_id=task_id, full=True)
    return not snapshot.running


async def test_tasks_are_isolated_per_conversation(tmp_path: Path) -> None:
    """别的会话拿着 task_id 也读不到——和浏览器 session 同一条边界。"""

    manager = _manager()
    mine, theirs = uuid4(), uuid4()
    try:
        started = await manager.start(
            conversation_id=mine, command=_command("sleep 5"), cwd=tmp_path
        )
        with pytest.raises(ShellTaskError, match="不存在"):
            await manager.read(conversation_id=theirs, task_id=started.task_id)
        with pytest.raises(ShellTaskError, match="不存在"):
            await manager.kill(conversation_id=theirs, task_id=started.task_id)
    finally:
        await manager.aclose()


async def test_unknown_task_tells_the_model_that_restarts_lose_tasks(tmp_path: Path) -> None:
    """失败必须是显式的：进程活在 worker 内存里，重启后就没了。"""

    manager = _manager()
    try:
        with pytest.raises(ShellTaskError, match="worker 重启"):
            await manager.read(conversation_id=uuid4(), task_id="不存在的任务")
    finally:
        await manager.aclose()


async def test_concurrent_task_limit_is_enforced_per_conversation(tmp_path: Path) -> None:
    manager = _manager(max_tasks_per_conversation=1)
    conversation_id = uuid4()
    try:
        first = await manager.start(
            conversation_id=conversation_id, command=_command("sleep 5"), cwd=tmp_path
        )
        with pytest.raises(ShellTaskError, match="上限 1"):
            await manager.start(
                conversation_id=conversation_id, command=_command("sleep 5"), cwd=tmp_path
            )
        # 结束之后名额立刻释放。
        await manager.kill(conversation_id=conversation_id, task_id=first.task_id)
        again = await manager.start(
            conversation_id=conversation_id, command=_command("sleep 5"), cwd=tmp_path
        )
        assert again.task_id != first.task_id
    finally:
        await manager.aclose()


async def test_kill_takes_down_the_whole_process_group(tmp_path: Path) -> None:
    """dev server 常常自己再 fork 一层；只杀父进程会留下占着端口的孤儿。"""

    marker = tmp_path / "child-alive"
    manager = _manager()
    conversation_id = uuid4()
    try:
        started = await manager.start(
            conversation_id=conversation_id,
            command=_command(
                f"sh -c 'sh -c \"while true; do touch {marker}; sleep 0.05; done\" & wait'"
            ),
            cwd=tmp_path,
        )
        await _wait_until(lambda: _exists(marker))
        killed = await manager.kill(conversation_id=conversation_id, task_id=started.task_id)
        assert killed.running is False

        marker.unlink(missing_ok=True)
        await asyncio.sleep(0.4)
        assert not marker.exists(), "子进程仍在运行，说明只杀了进程组的父进程"
    finally:
        await manager.aclose()


async def _exists(path: Path) -> bool:
    return await asyncio.to_thread(path.exists)


async def test_output_is_bounded_and_flagged_as_truncated(tmp_path: Path) -> None:
    manager = _manager(output_max_bytes=1024)
    conversation_id = uuid4()
    try:
        started = await manager.start(
            conversation_id=conversation_id,
            command=_command("sh -c 'for i in $(seq 1 500); do echo 0123456789; done'"),
            cwd=tmp_path,
        )
        await _wait_until(lambda: _finished(manager, conversation_id, started.task_id))
        snapshot = await manager.read(
            conversation_id=conversation_id, task_id=started.task_id, full=True
        )
        assert snapshot.output_truncated is True
        assert len(snapshot.output.encode("utf-8")) <= 1024
    finally:
        await manager.aclose()


async def test_expired_tasks_are_reaped_without_another_operation(tmp_path: Path) -> None:
    """没人再操作时绝对上限也必须主动回收，不能依赖下一次 start。"""

    manager = _manager(hard_ttl_s=0.01)
    conversation_id = uuid4()
    try:
        stale = await manager.start(
            conversation_id=conversation_id, command=_command("sleep 30"), cwd=tmp_path
        )
        await asyncio.sleep(0.1)
        with pytest.raises(ShellTaskError):
            await manager.read(conversation_id=conversation_id, task_id=stale.task_id)
        assert await manager.has_live_tasks(conversation_id) is False
    finally:
        await manager.aclose()


async def test_global_live_task_limit_covers_multiple_conversations(tmp_path: Path) -> None:
    manager = _manager(max_tasks_per_conversation=1, max_tasks_total=1)
    try:
        await manager.start(conversation_id=uuid4(), command=_command("sleep 30"), cwd=tmp_path)
        with pytest.raises(ShellTaskError, match="全局运行数已达上限 1"):
            await manager.start(conversation_id=uuid4(), command=_command("sleep 30"), cwd=tmp_path)
    finally:
        await manager.aclose()


async def test_completed_task_snapshots_are_bounded(tmp_path: Path) -> None:
    manager = _manager(
        max_tasks_per_conversation=1,
        max_tasks_total=1,
        max_retained_tasks=2,
    )
    conversation_id = uuid4()
    try:
        task_ids: list[str] = []
        for _ in range(3):
            started = await manager.start(
                conversation_id=conversation_id,
                command=_command("sh -c 'exit 0'"),
                cwd=tmp_path,
            )
            task_ids.append(started.task_id)
            await manager.wait(
                conversation_id=conversation_id,
                task_id=started.task_id,
                timeout_s=5.0,
            )

        with pytest.raises(ShellTaskError, match="不存在"):
            await manager.read(conversation_id=conversation_id, task_id=task_ids[0])
        assert (
            await manager.read(
                conversation_id=conversation_id,
                task_id=task_ids[-1],
                full=True,
            )
        ).running is False
    finally:
        await manager.aclose()


async def test_wake_on_returns_when_task_exits_and_carries_the_tail_output() -> None:
    """wake_on 的价值全在这一条：任务结束的那一刻返回，而且不丢最后一段输出。

    轮询做不到这两件事——醒来的时刻和进程退出的时刻永远对不齐，而最后一段输出往往
    正是失败原因。
    """

    manager = _manager()
    conversation_id = uuid4()
    started = await manager.start(
        conversation_id=conversation_id,
        command=_command("sh -c 'sleep 0.2; echo done-tail; exit 3'"),
        cwd=Path.cwd(),
    )
    snapshot = await manager.wait(
        conversation_id=conversation_id, task_id=started.task_id, timeout_s=10.0
    )
    assert snapshot.running is False
    assert snapshot.exit_code == 3
    assert "done-tail" in snapshot.output
    await manager.aclose()


async def test_wake_on_gives_up_at_the_timeout_without_killing_the_task() -> None:
    """超时只是"我不等了"，不是"把它收掉"：任务归 shell_task_kill 管。"""

    manager = _manager()
    conversation_id = uuid4()
    started = await manager.start(
        conversation_id=conversation_id,
        command=_command("sh -c 'sleep 30'"),
        cwd=Path.cwd(),
    )
    snapshot = await manager.wait(
        conversation_id=conversation_id, task_id=started.task_id, timeout_s=0.2
    )
    assert snapshot.running is True
    assert snapshot.exit_code is None
    assert await manager.has_live_tasks(conversation_id) is True
    await manager.aclose()


async def test_wake_on_returns_early_when_the_run_is_cancelled() -> None:
    """取消必须能穿透等待，否则一个 30 分钟的 wake_on 就是一个 30 分钟没法取消的 run。"""

    manager = _manager()
    conversation_id = uuid4()
    started = await manager.start(
        conversation_id=conversation_id,
        command=_command("sh -c 'sleep 30'"),
        cwd=Path.cwd(),
    )
    cancel_event = asyncio.Event()

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.1)
        cancel_event.set()

    canceller = asyncio.create_task(_cancel_soon())
    snapshot = await asyncio.wait_for(
        manager.wait(
            conversation_id=conversation_id,
            task_id=started.task_id,
            timeout_s=30.0,
            cancel_event=cancel_event,
        ),
        timeout=5.0,
    )
    assert snapshot.running is True
    await canceller
    await manager.aclose()


async def test_wake_on_refuses_a_task_from_another_conversation() -> None:
    manager = _manager()
    owner = uuid4()
    started = await manager.start(
        conversation_id=owner, command=_command("sh -c 'sleep 5'"), cwd=Path.cwd()
    )
    with pytest.raises(ShellTaskError, match="不存在"):
        await manager.wait(conversation_id=uuid4(), task_id=started.task_id, timeout_s=1.0)
    await manager.aclose()


async def test_has_live_tasks_only_counts_running_tasks_in_this_conversation() -> None:
    """sleep 的守卫用它做判据，所以"已经退出的任务不算活着"这条必须成立——
    否则跑完一条后台命令就再也不能 sleep 了。"""

    manager = _manager()
    conversation_id = uuid4()
    assert await manager.has_live_tasks(conversation_id) is False
    started = await manager.start(
        conversation_id=conversation_id,
        command=_command("sh -c 'exit 0'"),
        cwd=Path.cwd(),
    )
    await manager.wait(conversation_id=conversation_id, task_id=started.task_id, timeout_s=5.0)
    assert await manager.has_live_tasks(conversation_id) is False
    assert await manager.has_live_tasks(uuid4()) is False
    await manager.aclose()
