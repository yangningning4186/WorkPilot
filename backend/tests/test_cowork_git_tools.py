"""只读 Git 视图。

三条工具本身很薄，值得锁住的是两个容易悄悄错掉的地方：

1. **输出必须按已授权目录收窄。** `git -C <子目录>` 会一路找到仓库根，直接跑就会把
   用户没授权的那半个仓库的差异吐出来。
2. **不在仓库里不是错误。** Cowork 的工作目录经常只是个文档文件夹；那种情况要给一句
   模型能照着做的话，而不是一个异常。
"""

import subprocess
from pathlib import Path

import pytest

from app.cowork.git_tools import CoworkGitError, git_diff, git_log, git_status

pytestmark = pytest.mark.integration

_MAX_BYTES = 64 * 1024


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(root),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "granted").mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "granted" / "a.txt").write_text("one\n", encoding="utf-8")
    (tmp_path / "outside" / "secret.txt").write_text("classified\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "初始提交")
    return tmp_path


async def test_status_and_diff_stay_inside_the_granted_directory(repository: Path) -> None:
    (repository / "granted" / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    (repository / "outside" / "secret.txt").write_text("leaked\n", encoding="utf-8")

    status = await git_status(repository / "granted", max_bytes=_MAX_BYTES)
    assert status["is_repository"] is True
    assert status["branch"] == "main"
    assert status["clean"] is False
    entries = status["entries"]
    assert isinstance(entries, list)
    assert any("granted/a.txt" in entry for entry in entries)
    # 授权目录之外的改动一条都不能出现——这正是加 `-- <目录>` pathspec 的原因。
    assert not any("outside" in entry for entry in entries)

    diff = await git_diff(
        repository / "granted", staged=False, stat_only=False, max_bytes=_MAX_BYTES
    )
    assert "granted/a.txt" in diff["diff"]
    assert "leaked" not in diff["diff"]
    assert diff["empty"] is False


async def test_diff_reports_staged_and_stat_only_variants(repository: Path) -> None:
    (repository / "granted" / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(repository, "add", "granted/a.txt")

    unstaged = await git_diff(
        repository / "granted", staged=False, stat_only=False, max_bytes=_MAX_BYTES
    )
    assert unstaged["empty"] is True

    staged = await git_diff(
        repository / "granted", staged=True, stat_only=True, max_bytes=_MAX_BYTES
    )
    assert staged["stat_only"] is True
    assert "granted/a.txt" in staged["diff"]
    # stat 模式下不该出现补丁正文。
    assert "+two" not in staged["diff"]


async def test_log_parses_records_and_scopes_to_the_granted_directory(
    repository: Path,
) -> None:
    (repository / "outside" / "secret.txt").write_text("changed\n", encoding="utf-8")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-qm", "只动了未授权目录")

    log = await git_log(repository / "granted", max_count=10, max_bytes=_MAX_BYTES)
    commits = log["commits"]
    assert isinstance(commits, list)
    subjects = [commit["subject"] for commit in commits]
    assert subjects == ["初始提交"]
    assert commits[0]["author"] == "t"
    assert len(commits[0]["sha"]) >= 7


async def test_a_plain_folder_is_reported_as_not_a_repository(tmp_path: Path) -> None:
    """Cowork 的工作目录经常只是个文档文件夹。这条路径必须给话，不能抛异常。"""

    for result in (
        await git_status(tmp_path, max_bytes=_MAX_BYTES),
        await git_diff(tmp_path, staged=False, stat_only=False, max_bytes=_MAX_BYTES),
        await git_log(tmp_path, max_count=5, max_bytes=_MAX_BYTES),
    ):
        assert result["is_repository"] is False
        assert "不在 Git 仓库里" in str(result["note"])


async def test_a_file_path_falls_back_to_its_parent_directory(repository: Path) -> None:
    status = await git_status(repository / "granted" / "a.txt", max_bytes=_MAX_BYTES)
    assert status["is_repository"] is True
    assert status["path"] == str(repository / "granted")


async def test_a_missing_directory_is_an_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(CoworkGitError, match="目录不存在"):
        await git_status(tmp_path / "nope", max_bytes=_MAX_BYTES)
