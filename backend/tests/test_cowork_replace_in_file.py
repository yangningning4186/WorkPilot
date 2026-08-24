"""局部替换：改文件的一部分而不是整份重写。

这条路径解决的是正确性问题。`baseline_sha256` 挡的是"文件在你读之后被别人改了"，
挡不住"你只读了前 500 行就重写整个文件"——后者校验完全通过，而文件后半段被静默丢掉。
"""

from pathlib import Path

import pytest

from app.core.config import Settings
from app.cowork.files import CoworkFileError, replace_in_file
from app.cowork.tools import build_default_cowork_registry

pytestmark = pytest.mark.integration

_SETTINGS = Settings()


def _write(path: Path, text: str) -> str:
    import hashlib

    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def test_replacement_leaves_the_rest_of_the_file_byte_identical(tmp_path: Path) -> None:
    target = tmp_path / "report.md"
    original = "# 标题\n\n第一段\n\n待改的一行\n\n最后一段\n"
    baseline = _write(target, original)

    result = await replace_in_file(
        target,
        old_text="待改的一行",
        new_text="改好的一行",
        baseline_sha256=baseline,
        expected_count=None,
        settings=_SETTINGS,
    )

    assert result.replacements == 1
    assert target.read_text(encoding="utf-8") == original.replace("待改的一行", "改好的一行")
    # 走的是和 write_text_file 同一条原子写路径，所以照样留备份。
    assert result.backup_path is not None and result.backup_path.exists()


async def test_ambiguous_match_is_refused_instead_of_editing_the_wrong_one(
    tmp_path: Path,
) -> None:
    """改错位置比报错贵得多，而模型看不出自己改的是第几处。"""

    target = tmp_path / "config.txt"
    baseline = _write(target, "timeout = 30\nretries = 3\ntimeout = 30\n")

    with pytest.raises(CoworkFileError, match="出现 2 次"):
        await replace_in_file(
            target,
            old_text="timeout = 30",
            new_text="timeout = 60",
            baseline_sha256=baseline,
            expected_count=None,
            settings=_SETTINGS,
        )
    assert target.read_text(encoding="utf-8").count("timeout = 30") == 2

    # 明确说了要改两处才放行。
    result = await replace_in_file(
        target,
        old_text="timeout = 30",
        new_text="timeout = 60",
        baseline_sha256=baseline,
        expected_count=2,
        settings=_SETTINGS,
    )
    assert result.replacements == 2
    assert "timeout = 30" not in target.read_text(encoding="utf-8")


async def test_missing_text_tells_the_model_how_to_recover(tmp_path: Path) -> None:
    """面向模型的错误信息是可执行指令，不是 stack trace（约束 4）。"""

    target = tmp_path / "notes.md"
    baseline = _write(target, "只有这一行\n")

    with pytest.raises(CoworkFileError, match="逐字复制"):
        await replace_in_file(
            target,
            old_text="不存在的片段",
            new_text="x",
            baseline_sha256=baseline,
            expected_count=None,
            settings=_SETTINGS,
        )


async def test_stale_baseline_and_noop_edits_are_refused(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    _write(target, "原始\n")
    stale = "0" * 64

    with pytest.raises(CoworkFileError, match="发生变化"):
        await replace_in_file(
            target,
            old_text="原始",
            new_text="新的",
            baseline_sha256=stale,
            expected_count=None,
            settings=_SETTINGS,
        )

    fresh = _write(target, "原始\n")
    with pytest.raises(CoworkFileError, match="不会产生任何改动"):
        await replace_in_file(
            target,
            old_text="原始",
            new_text="原始",
            baseline_sha256=fresh,
            expected_count=None,
            settings=_SETTINGS,
        )


async def test_binary_documents_stay_on_their_own_tools(tmp_path: Path) -> None:
    target = tmp_path / "report.docx"
    baseline = _write(target, "看起来像文本")

    with pytest.raises(CoworkFileError, match="专用工具"):
        await replace_in_file(
            target,
            old_text="像",
            new_text="不像",
            baseline_sha256=baseline,
            expected_count=None,
            settings=_SETTINGS,
        )


def test_replace_in_file_is_a_core_tool_with_write_capability() -> None:
    """局部编辑是日常动作，不该靠话题命中才出现在目录里。"""

    registry = build_default_cowork_registry()
    spec = registry.get("replace_in_file")

    assert spec.capability == "filesystem.write"
    assert spec.risk == "write" and spec.effect == "filesystem"
    assert spec.path_argument == "path"
    assert "replace_in_file" in {
        item.name for item in registry.tool_definitions_for("随便一个无关话题")
    }
