import asyncio
import hashlib
import subprocess
from pathlib import Path

import fitz
import pytest

from app.core.config import Settings
from app.cowork.files import (
    CoworkFileError,
    list_files,
    read_pdf_file,
    read_text_file,
    ripgrep_path,
    search_files,
    write_text_file,
)


async def test_text_read_write_requires_baseline_and_keeps_bounded_backups(
    tmp_path: Path,
) -> None:
    target = tmp_path / "notes.md"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    settings = Settings(
        cowork_file_read_max_bytes=1024,
        cowork_file_write_max_bytes=1024,
        workspace_backup_versions_per_file=2,
    )

    snapshot = await read_text_file(target, start_line=2, max_lines=1, max_bytes=1024)
    assert snapshot.content == "two\n"
    assert snapshot.total_lines == 3
    assert snapshot.truncated is True
    assert snapshot.sha256 == hashlib.sha256(target.read_bytes()).hexdigest()

    with pytest.raises(CoworkFileError, match="baseline_sha256"):
        await write_text_file(target, content="changed", baseline_sha256=None, settings=settings)
    with pytest.raises(CoworkFileError, match="发生变化"):
        await write_text_file(target, content="changed", baseline_sha256="0" * 64, settings=settings)

    result = await write_text_file(
        target,
        content="changed\n",
        baseline_sha256=snapshot.sha256,
        settings=settings,
    )
    assert result.created is False
    assert result.backup_path is not None and result.backup_path.is_file()
    assert target.read_text(encoding="utf-8") == "changed\n"

    for content in ("second\n", "third\n"):
        current = await read_text_file(target, start_line=1, max_lines=10, max_bytes=1024)
        await write_text_file(
            target,
            content=content,
            baseline_sha256=current.sha256,
            settings=settings,
        )
    assert len(list((tmp_path / ".workpilot-backups").glob("notes.md.*.bak"))) == 2


async def test_text_write_can_explicitly_create_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "reports" / "2026-08" / "summary.md"
    settings = Settings(cowork_file_write_max_bytes=1024)

    with pytest.raises(CoworkFileError, match="create_parents=true"):
        await write_text_file(
            target,
            content="# Summary\n",
            baseline_sha256=None,
            settings=settings,
        )
    assert target.parent.exists() is False

    result = await write_text_file(
        target,
        content="# Summary\n",
        baseline_sha256=None,
        create_parents=True,
        settings=settings,
    )

    assert result.created is True
    assert target.read_text(encoding="utf-8") == "# Summary\n"


async def test_list_and_search_are_bounded_and_skip_hidden_binary_and_dependencies(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("Alpha\nneedle here\n", encoding="utf-8")
    (tmp_path / "name-needle.txt").write_text("nothing", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\x00needle")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "secret.txt").write_text("needle", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "package.js").write_text("needle", encoding="utf-8")

    items, truncated = await list_files(
        tmp_path,
        recursive=True,
        pattern="*.md",
        max_results=10,
        max_scan_entries=20,
    )
    assert [item.relative_path for item in items] == ["docs/guide.md"]
    assert truncated is False

    matches, truncated, scanned = await search_files(
        tmp_path,
        query="needle",
        pattern="*",
        case_sensitive=False,
        max_results=10,
        max_scan_entries=20,
        max_file_bytes=1024,
    )
    assert {(item.relative_path, item.matched_in) for item in matches} == {
        ("docs/guide.md", "content"),
        ("name-needle.txt", "path"),
    }
    assert truncated is False
    assert scanned == 3


async def test_read_text_rejects_binary_and_oversized_files(tmp_path: Path) -> None:
    binary = tmp_path / "binary.dat"
    binary.write_bytes(b"abc\x00def")
    with pytest.raises(CoworkFileError, match="二进制"):
        await read_text_file(binary, start_line=1, max_lines=10, max_bytes=100)

    large = tmp_path / "large.txt"
    large.write_text("x" * 101, encoding="utf-8")
    with pytest.raises(CoworkFileError, match="超过读取上限"):
        await read_text_file(large, start_line=1, max_lines=10, max_bytes=100)

    with pytest.raises(CoworkFileError, match="二进制文档"):
        await write_text_file(
            tmp_path / "fake.pdf",
            content="not a pdf",
            baseline_sha256=None,
            settings=Settings(cowork_file_write_max_bytes=1024),
        )


async def test_read_pdf_uses_existing_parser_and_returns_bounded_text(tmp_path: Path) -> None:
    pdf_path = tmp_path / "brief.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_textbox(
        fitz.Rect(72, 72, 540, 770),
        "WorkPilot " + ("PDF content " * 150),
        fontsize=8,
    )
    document.save(pdf_path)
    document.close()

    snapshot = await read_pdf_file(
        pdf_path,
        settings=Settings(
            pdf_parser_mode="pymupdf",
            pdf_parse_timeout_s=30,
            pdf_max_bytes=1024 * 1024,
            cowork_pdf_text_max_chars=1000,
        ),
    )

    assert snapshot.page_count == 1
    assert snapshot.content.startswith("WorkPilot PDF content")
    assert len(snapshot.content) == 1000
    assert snapshot.truncated is True
    assert snapshot.parser == "pymupdf"


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)


async def test_search_respects_gitignore_when_ripgrep_is_available(tmp_path: Path) -> None:
    """.gitignore 感知是换 ripgrep 的主要收益之一。

    纯 Python 那条路会把构建产物、vendored 依赖逐字节读一遍再当成命中回给模型；在真实
    仓库里这既是几十倍的耗时，也是一大批噪声命中。这里锁住的是"被忽略的文件不出现在
    结果里"这个语义本身。
    """

    if ripgrep_path() is None:
        pytest.skip("这台机器没有 ripgrep")
    # ripgrep 只在 git 仓库里才认 `.gitignore`（仓库外要用 `.ignore`），
    # 所以这条用例必须真的建一个仓库，否则它验证的不是 gitignore 感知。
    await asyncio.to_thread(_git_init, tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "bundle.js").write_text("needle\n", encoding="utf-8")
    (tmp_path / "src.txt").write_text("needle\n", encoding="utf-8")

    matches, _, _ = await search_files(
        tmp_path,
        query="needle",
        pattern="*",
        case_sensitive=False,
        max_results=10,
        max_scan_entries=100,
        max_file_bytes=1024,
    )
    assert {item.relative_path for item in matches} == {"src.txt"}


async def test_search_treats_the_query_as_a_literal_not_a_regex(tmp_path: Path) -> None:
    """模型给的是"要找的字符串"。不加字面匹配开关，一个 `.` 或 `(` 就会改掉语义。"""

    (tmp_path / "a.txt").write_text("value = f(x)\nvalueXX\n", encoding="utf-8")
    matches, _, _ = await search_files(
        tmp_path,
        query="f(x)",
        pattern="*",
        case_sensitive=False,
        max_results=10,
        max_scan_entries=100,
        max_file_bytes=1024,
    )
    assert [item.line for item in matches] == [1]
