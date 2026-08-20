"""Cowork 通用文件读写、目录列举、文本搜索与 PDF 读取。"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import shutil
import stat as stat_module
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings
from app.ingest.pdf import parse_pdf
from app.ingest.settings import pdf_parser_config_from_settings

_SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".workpilot-backups",
        "__pycache__",
        "node_modules",
    }
)
_KNOWN_BINARY_SUFFIXES = frozenset(
    {
        ".doc",
        ".docx",
        ".gif",
        ".jpeg",
        ".jpg",
        ".ods",
        ".pdf",
        ".png",
        ".ppt",
        ".pptx",
        ".webp",
        ".xls",
        ".xlsm",
        ".xlsx",
        ".zip",
    }
)


class CoworkFileError(RuntimeError):
    pass


@dataclass(frozen=True)
class TextFileSnapshot:
    path: Path
    sha256: str
    content: str
    size_bytes: int
    total_lines: int
    start_line: int
    end_line: int
    truncated: bool


@dataclass(frozen=True)
class TextFileWriteResult:
    path: Path
    sha256: str
    size_bytes: int
    created: bool
    backup_path: Path | None


@dataclass(frozen=True)
class FileListItem:
    path: Path
    relative_path: str
    kind: str
    size_bytes: int
    modified_at_ns: int


@dataclass(frozen=True)
class FileSearchMatch:
    path: Path
    relative_path: str
    line: int | None
    preview: str
    matched_in: str


@dataclass(frozen=True)
class PdfSnapshot:
    path: Path
    title: str
    parser: str
    page_count: int
    content: str
    truncated: bool
    quality: dict[str, object]


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_bounded(path: Path, max_bytes: int) -> bytes:
    try:
        stat = path.stat()
    except OSError as error:
        raise CoworkFileError(f"无法读取文件信息: {path}") from error
    if not path.is_file():
        raise CoworkFileError(f"目标不是普通文件: {path}")
    if stat.st_size > max_bytes:
        raise CoworkFileError(f"文件大小 {stat.st_size} bytes 超过读取上限 {max_bytes} bytes")
    try:
        with path.open("rb") as stream:
            content = stream.read(max_bytes + 1)
    except OSError as error:
        raise CoworkFileError(f"无法读取文件: {path}") from error
    if len(content) > max_bytes:
        raise CoworkFileError(f"文件在读取期间超过上限 {max_bytes} bytes")
    return content


def _decode_text(content: bytes, path: Path) -> str:
    if b"\x00" in content:
        raise CoworkFileError(f"文件似乎是二进制格式，不能作为通用文本读取: {path}")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise CoworkFileError(f"文件不是 UTF-8 文本: {path}") from error


async def read_text_file(
    path: Path,
    *,
    start_line: int,
    max_lines: int,
    max_bytes: int,
) -> TextFileSnapshot:
    return await asyncio.to_thread(
        _read_text_file_sync,
        path,
        start_line=start_line,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def _read_text_file_sync(
    path: Path,
    *,
    start_line: int,
    max_lines: int,
    max_bytes: int,
) -> TextFileSnapshot:
    raw = _read_bounded(path, max_bytes)
    text = _decode_text(raw, path)
    lines = text.splitlines(keepends=True)
    total_lines = len(lines)
    start_index = min(start_line - 1, total_lines)
    selected = lines[start_index : start_index + max_lines]
    end_line = start_index + len(selected)
    return TextFileSnapshot(
        path=path,
        sha256=_sha256_bytes(raw),
        content="".join(selected),
        size_bytes=len(raw),
        total_lines=total_lines,
        start_line=start_index + 1,
        end_line=end_line,
        truncated=end_line < total_lines,
    )


async def list_files(
    root: Path,
    *,
    recursive: bool,
    pattern: str,
    max_results: int,
    max_scan_entries: int,
) -> tuple[list[FileListItem], bool]:
    return await asyncio.to_thread(
        _list_files_sync,
        root,
        recursive=recursive,
        pattern=pattern,
        max_results=max_results,
        max_scan_entries=max_scan_entries,
    )


def _list_files_sync(
    root: Path,
    *,
    recursive: bool,
    pattern: str,
    max_results: int,
    max_scan_entries: int,
) -> tuple[list[FileListItem], bool]:
    if not root.is_dir():
        raise CoworkFileError(f"目录不存在: {root}")
    items: list[FileListItem] = []
    scanned = 0
    truncated = False
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if name not in _SKIPPED_DIRECTORIES and not name.startswith(".")
        )
        names = sorted(files)
        if not recursive:
            directories[:] = []
        for name in names:
            scanned += 1
            if scanned > max_scan_entries:
                return items, True
            path = Path(current) / name
            if path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            if not fnmatch.fnmatch(relative, pattern) and not fnmatch.fnmatch(name, pattern):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append(
                FileListItem(
                    path=path,
                    relative_path=relative,
                    kind="file",
                    size_bytes=stat.st_size,
                    modified_at_ns=stat.st_mtime_ns,
                )
            )
            if len(items) >= max_results:
                truncated = True
                return items, truncated
    return items, truncated


async def search_files(
    root: Path,
    *,
    query: str,
    pattern: str,
    case_sensitive: bool,
    max_results: int,
    max_scan_entries: int,
    max_file_bytes: int,
) -> tuple[list[FileSearchMatch], bool, int]:
    return await asyncio.to_thread(
        _search_files_sync,
        root,
        query=query,
        pattern=pattern,
        case_sensitive=case_sensitive,
        max_results=max_results,
        max_scan_entries=max_scan_entries,
        max_file_bytes=max_file_bytes,
    )


def _search_files_sync(
    root: Path,
    *,
    query: str,
    pattern: str,
    case_sensitive: bool,
    max_results: int,
    max_scan_entries: int,
    max_file_bytes: int,
) -> tuple[list[FileSearchMatch], bool, int]:
    if not root.is_dir():
        raise CoworkFileError(f"搜索根目录不存在: {root}")
    needle = query if case_sensitive else query.casefold()
    matches: list[FileSearchMatch] = []
    scanned = 0
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if name not in _SKIPPED_DIRECTORIES and not name.startswith(".")
        )
        for name in sorted(files):
            scanned += 1
            if scanned > max_scan_entries:
                return matches, True, scanned - 1
            path = Path(current) / name
            if path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            if not fnmatch.fnmatch(relative, pattern) and not fnmatch.fnmatch(name, pattern):
                continue
            comparable_path = relative if case_sensitive else relative.casefold()
            if needle in comparable_path:
                matches.append(
                    FileSearchMatch(
                        path=path,
                        relative_path=relative,
                        line=None,
                        preview=relative,
                        matched_in="path",
                    )
                )
                if len(matches) >= max_results:
                    return matches, True, scanned
            try:
                raw = _read_bounded(path, max_file_bytes)
                text = _decode_text(raw, path)
            except CoworkFileError:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                comparable_line = line if case_sensitive else line.casefold()
                if needle not in comparable_line:
                    continue
                matches.append(
                    FileSearchMatch(
                        path=path,
                        relative_path=relative,
                        line=line_number,
                        preview=line.strip()[:500],
                        matched_in="content",
                    )
                )
                if len(matches) >= max_results:
                    return matches, True, scanned
    return matches, False, scanned


async def write_text_file(
    path: Path,
    *,
    content: str,
    baseline_sha256: str | None,
    settings: Settings,
) -> TextFileWriteResult:
    return await asyncio.to_thread(
        _write_text_file_sync,
        path,
        content=content,
        baseline_sha256=baseline_sha256,
        max_bytes=settings.cowork_file_write_max_bytes,
        backup_versions=settings.workspace_backup_versions_per_file,
    )


def _write_text_file_sync(
    path: Path,
    *,
    content: str,
    baseline_sha256: str | None,
    max_bytes: int,
    backup_versions: int,
) -> TextFileWriteResult:
    if path.suffix.casefold() in _KNOWN_BINARY_SUFFIXES:
        raise CoworkFileError(f"通用文本工具不能写入 {path.suffix} 二进制文档，请使用对应专用工具")
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        raise CoworkFileError(f"写入内容 {len(encoded)} bytes 超过上限 {max_bytes} bytes")
    parent = path.parent
    if not parent.is_dir():
        raise CoworkFileError("目标文件的父目录不存在")
    created = not path.exists()
    backup: Path | None = None
    previous_mode: int | None = None
    if not created:
        if path.is_symlink() or not path.is_file():
            raise CoworkFileError("目标必须是普通文件，不能是符号链接或目录")
        previous = _read_bounded(path, max_bytes)
        actual_sha256 = _sha256_bytes(previous)
        if baseline_sha256 is None:
            raise CoworkFileError("覆盖现有文件必须提供 read_text_file 返回的 baseline_sha256")
        if actual_sha256 != baseline_sha256:
            raise CoworkFileError("文件已在读取后发生变化，请重新读取后再写入")
        previous_mode = stat_module.S_IMODE(path.stat().st_mode)
        backup = create_file_backup(path, backup_versions)
    elif baseline_sha256 is not None:
        raise CoworkFileError("目标文件尚不存在，baseline_sha256 必须省略")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if previous_mode is not None:
            os.chmod(temporary, previous_mode)
        if created:
            if path.exists():
                raise CoworkFileError("目标文件已在写入期间被创建，请重新读取")
        else:
            current = _read_bounded(path, max_bytes)
            if _sha256_bytes(current) != baseline_sha256:
                raise CoworkFileError("文件在写入期间发生变化，请重新读取")
        os.replace(temporary, path)
        try:
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Windows 不允许直接 fsync 目录；文件本身已在 replace 前 fsync。
            pass
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return TextFileWriteResult(
        path=path,
        sha256=_sha256_bytes(encoded),
        size_bytes=len(encoded),
        created=created,
        backup_path=backup,
    )


def create_file_backup(path: Path, versions: int) -> Path:
    backup_root = path.parent / ".workpilot-backups"
    if backup_root.exists() and backup_root.is_symlink():
        raise CoworkFileError("备份目录不能是符号链接")
    backup_root.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:12]
    backup = backup_root / f"{path.name}.{digest}.{stamp}.bak"
    shutil.copy2(path, backup)
    existing = sorted(backup_root.glob(f"{path.name}.{digest}.*.bak"), reverse=True)
    for stale in existing[versions:]:
        stale.unlink(missing_ok=True)
    return backup


async def read_pdf_file(path: Path, *, settings: Settings) -> PdfSnapshot:
    try:
        stat = await asyncio.to_thread(path.stat)
    except OSError as error:
        raise CoworkFileError(f"PDF 不存在: {path}") from error
    if path.suffix.casefold() != ".pdf" or not stat_module.S_ISREG(stat.st_mode):
        raise CoworkFileError("read_pdf 只接受现有 .pdf 文件")
    if stat.st_size > settings.pdf_max_bytes:
        raise CoworkFileError(
            f"PDF 大小 {stat.st_size} bytes 超过上限 {settings.pdf_max_bytes} bytes"
        )
    parsed = await parse_pdf(path, pdf_parser_config_from_settings(settings))
    limit = settings.cowork_pdf_text_max_chars
    content = parsed.document.full_text
    return PdfSnapshot(
        path=path,
        title=parsed.title,
        parser=parsed.parser,
        page_count=parsed.document.page_count or 0,
        content=content[:limit],
        truncated=len(content) > limit,
        quality=parsed.quality.to_dict(),
    )
