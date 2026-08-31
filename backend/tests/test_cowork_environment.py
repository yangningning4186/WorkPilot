"""注入给模型的运行环境事实。

分界不是按主题，而是按「这条事实在一次 run 里会不会变」：不变的进 system prompt
（provider 前缀缓存从第 0 条消息起算），会变的挂在 outbound 末尾的临时块里。
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from app.cowork.environment import (
    render_capabilities_block,
    render_environment_block,
    render_roots_block,
)
from app.cowork_policy import ALL_CAPABILITIES


def test_environment_block_states_the_date_so_relative_time_is_not_guessed() -> None:
    block = render_environment_block(datetime(2026, 8, 21, 3, 30, tzinfo=UTC))

    assert "<environment>" in block
    assert "2026-08-2" in block
    # 没有日期，"上个季度"只能靠猜；没有系统名，BSD/GNU 的 sed 参数也只能靠猜。
    assert "操作系统" in block


def test_environment_block_is_byte_stable_for_one_run() -> None:
    """同一个起始时刻必须逐字相同——它进 system prompt，改一个字整段前缀缓存作废。"""

    now = datetime(2026, 8, 21, 3, 30, tzinfo=UTC)
    assert render_environment_block(now) == render_environment_block(now)


def test_roots_block_marks_the_default_output_directory() -> None:
    roots = [
        SimpleNamespace(canonical_path="/work/a", access_mode="read_write"),
        SimpleNamespace(canonical_path="/work/b", access_mode="read_only"),
    ]

    block = render_roots_block(roots)

    assert "/work/a" in block and "读写" in block
    assert "/work/b" in block and "只读" in block
    assert render_roots_block([]) == ""


def test_capabilities_block_tells_the_model_what_it_already_holds() -> None:
    """已授权却仍去 request_capability，run 会停在等人批准上——任务就此失败。"""

    block = render_capabilities_block(["network.read", "filesystem.read"], sorted(ALL_CAPABILITIES))

    assert "<capabilities>" in block
    assert "network.read" in block
    # 已授予的必须明确标成"不要再要一次"。
    held, missing = block.split("未授予")
    assert "network.read" in held and "filesystem.read" in held
    assert "shell.execute" in missing
    assert "network.read" not in missing


def test_capabilities_block_keeps_approval_separate_from_authorization() -> None:
    """有授权不等于免审批：run_shell 拿到 shell.execute 之后仍要逐次确认。"""

    block = render_capabilities_block(["host.execute"], sorted(ALL_CAPABILITIES))

    assert "已授予不等于跳过动作审核" in block


def test_capabilities_block_is_empty_when_there_is_nothing_to_say() -> None:
    assert render_capabilities_block([], []) == ""


def test_capabilities_block_says_shell_uses_action_level_approval() -> None:
    """模型不得再为 Shell 先拼一层 host.execute capability。"""

    block = render_capabilities_block(["host.execute"], sorted(ALL_CAPABILITIES))

    assert "run_shell 在真正执行命令时生成动作级审批" in block
    assert "不要自行拼 capability" in block
