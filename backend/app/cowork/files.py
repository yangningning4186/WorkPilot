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
class TextFileReplaceResult:
    path: Path
    sha256: str
    size_bytes: int
    backup_path: Path | None
    replacements: int


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


def ripgrep_path() -> str | None:
    """ripgrep 可执行文件，找不到时回落到纯 Python 扫描。

    结果不缓存：sidecar 可能在用户装完 ripgrep 之后还活着，缓存一个 None 会让它一直
    走慢路径。`which` 本身只是一次 PATH 查表，比一次目录遍历便宜好几个数量级。
    """

    return shutil.which("rg")


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
    """在目录里按文件名与文本内容搜索字面字符串。

    有 ripgrep 就用 ripgrep：它是多线程的、跳过二进制文件、并且尊重 `.gitignore`。
    纯 Python 那条路会把 `node_modules`、`target/`、构建产物全部逐字节读一遍，在真实
    仓库上是几十倍的差距，还会把一堆 vendored 代码当成命中回给模型。

    找不到 ripgrep 时回落到原来的实现——功能等价，只是慢，且不认 `.gitignore`。
    """

    executable = ripgrep_path()
    if executable is not None:
        try:
            return await _search_files_ripgrep(
                root,
                executable=executable,
                query=query,
                pattern=pattern,
                case_sensitive=case_sensitive,
                max_results=max_results,
                max_scan_entries=max_scan_entries,
                max_file_bytes=max_file_bytes,
            )
        except _RipgrepUnavailable:
            # ripgrep 在但跑不起来（版本太老、参数不认、被安全策略拦下）不该让搜索整个失败，
            # 回落到 Python 实现即可。真正的用法错误（正则、权限）不走这个分支。
            pass
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


class _RipgrepUnavailable(RuntimeError):
    pass


_RIPGREP_TIMEOUT_S = 30.0


async def _run_ripgrep(argv: tuple[str, ...], *, cwd: Path, max_bytes: int) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:  # pragma: no cover - which() 命中后再失败极罕见
        raise _RipgrepUnavailable(str(error)) from error
    assert process.stdout is not None and process.stderr is not None
    try:
        stdout, stderr, _ = await asyncio.wait_for(
            asyncio.gather(
                _read_stream_bounded(process.stdout, max_bytes),
                _read_stream_bounded(process.stderr, 8 * 1024),
                process.wait(),
            ),
            timeout=_RIPGREP_TIMEOUT_S,
        )
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise _RipgrepUnavailable("ripgrep 超时") from error
    # 0 = 有命中，1 = 无命中，其余都是 ripgrep 自己出了问题。
    if process.returncode not in (0, 1):
        raise _RipgrepUnavailable(stderr.decode("utf-8", errors="replace").strip())
    return stdout.decode("utf-8", errors="replace")


async def _read_stream_bounded(stream: asyncio.StreamReader, max_bytes: int) -> bytes:
    retained = bytearray()
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return bytes(retained)
        room = max_bytes - len(retained)
        if room > 0:
            retained.extend(chunk[:room])


async def _search_files_ripgrep(
    root: Path,
    *,
    executable: str,
    query: str,
    pattern: str,
    case_sensitive: bool,
    max_results: int,
    max_scan_entries: int,
    max_file_bytes: int,
) -> tuple[list[FileSearchMatch], bool, int]:
    if not await asyncio.to_thread(root.is_dir):
        raise CoworkFileError(f"搜索根目录不存在: {root}")

    # 这里**不**把 `pattern` 交给 `--glob`：ripgrep 的 include glob 是一层 override，
    # 命中它的文件会连 `.gitignore` 一起绕过（`--glob '*'` 会把整个 `build/` 列回来），
    # 而 gitignore 感知正是换 ripgrep 的主要收益。所以只用 `!` 形式的排除 glob，
    # `pattern` 留给 Python 侧过滤——顺带保住了「相对路径或文件名任一命中」这条
    # 与纯 Python 实现一致的语义（ripgrep 的 glob 不是这么匹配的）。
    common = (
        executable,
        "--no-config",  # 用户的 RIPGREP_CONFIG_PATH 可能开了 --hidden 之类的开关
        "--no-messages",
        "--color=never",
        *(f"--glob=!{name}/" for name in sorted(_SKIPPED_DIRECTORIES)),
    )

    def _matches_pattern(relative: str) -> bool:
        name = relative.rpartition("/")[2]
        return fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern)

    # 第一趟只列文件：它同时给出「按文件名命中」的候选集和 files_scanned 口径。
    listing = await _run_ripgrep((*common, "--files"), cwd=root, max_bytes=max_scan_entries * 4096)
    candidates = [line for line in listing.splitlines() if line]
    scan_truncated = len(candidates) > max_scan_entries
    relative_paths = sorted(
        path for path in candidates[:max_scan_entries] if _matches_pattern(path)
    )
    scanned = len(relative_paths)

    # 第二趟拿内容命中。`-F` 是字面匹配：模型给的是要找的字符串，不是正则，
    # 不加这个开关一个 `.` 或 `(` 就会把语义改掉。
    content = await _run_ripgrep(
        (
            *common,
            "--fixed-strings",
            "--with-filename",
            "--line-number",
            "--no-heading",
            # 路径和行号之间用 NUL 分隔：文件名里带冒号时，按冒号切会把路径切错。
            "--null",
            f"--max-filesize={max_file_bytes}",
            "--case-sensitive" if case_sensitive else "--ignore-case",
            "--",
            query,
        ),
        cwd=root,
        # 单条命中最长 500 字符，再乘以结果上限就够；留两倍余量给路径前缀。
        max_bytes=max_results * 2048 + 65_536,
    )
    hits: dict[str, list[tuple[int, str]]] = {}
    for line in content.splitlines():
        head, separator, rest = line.partition("\x00")
        if not separator:
            continue
        number, separator, text = rest.partition(":")
        if not separator or not number.isdigit():
            continue
        hits.setdefault(head, []).append((int(number), text))

    needle = query if case_sensitive else query.casefold()
    matches: list[FileSearchMatch] = []
    truncated = scan_truncated
    for relative in relative_paths:
        path = root / relative
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
        for line_number, text in hits.get(relative, ()):
            matches.append(
                FileSearchMatch(
                    path=path,
                    relative_path=relative,
                    line=line_number,
                    preview=text.strip()[:500],
                    matched_in="content",
                )
            )
            if len(matches) >= max_results:
                return matches, True, scanned
    return matches, truncated, scanned


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


async def replace_in_file(
    path: Path,
    *,
    old_text: str,
    new_text: str,
    baseline_sha256: str | None,
    expected_count: int | None,
    settings: Settings,
) -> TextFileReplaceResult:
    """按精确文本替换改写文件的一部分。

    存在的理由是正确性，不是省 token：全量覆盖要求模型手上有完整的当前内容，而它常常
    只读了前几百行就重写整个文件——后面的内容会被静默丢掉，`baseline_sha256` 一样能校验
    通过（它挡的是并发写，不是"你没读全"）。局部替换让没被匹配到的字节原样保留。
    """

    return await asyncio.to_thread(
        _replace_in_file_sync,
        path,
        old_text=old_text,
        new_text=new_text,
        baseline_sha256=baseline_sha256,
        expected_count=expected_count,
        max_bytes=settings.cowork_file_write_max_bytes,
        backup_versions=settings.workspace_backup_versions_per_file,
    )


def _replace_in_file_sync(
    path: Path,
    *,
    old_text: str,
    new_text: str,
    baseline_sha256: str | None,
    expected_count: int | None,
    max_bytes: int,
    backup_versions: int,
) -> TextFileReplaceResult:
    if path.suffix.casefold() in _KNOWN_BINARY_SUFFIXES:
        raise CoworkFileError(f"通用文本工具不能写入 {path.suffix} 二进制文档，请使用对应专用工具")
    if not old_text:
        raise CoworkFileError("old_text 不能为空；新建文件请使用 write_text_file")
    if old_text == new_text:
        raise CoworkFileError("old_text 与 new_text 相同，这次替换不会产生任何改动")
    if path.is_symlink() or not path.is_file():
        raise CoworkFileError("目标必须是已存在的普通文件，不能是符号链接或目录")
    previous = _read_bounded(path, max_bytes)
    actual_sha256 = _sha256_bytes(previous)
    if baseline_sha256 is None:
        raise CoworkFileError("修改现有文件必须提供 read_text_file 返回的 baseline_sha256")
    if actual_sha256 != baseline_sha256:
        raise CoworkFileError("文件已在读取后发生变化，请重新读取后再修改")
    try:
        text = previous.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CoworkFileError("目标文件不是 UTF-8 文本，无法做文本替换") from error

    found = text.count(old_text)
    if found == 0:
        raise CoworkFileError(
            "old_text 在文件中不存在。请先用 read_text_file 取回原文，"
            "逐字复制要替换的片段（包括缩进与换行），不要凭记忆书写"
        )
    # 默认要求唯一命中：改错位置比报错贵得多，而模型看不出自己改的是第几处。
    wanted = 1 if expected_count is None else expected_count
    if found != wanted:
        raise CoworkFileError(
            f"old_text 在文件中出现 {found} 次，与 expected_count={wanted} 不符。"
            "请扩大 old_text 的上下文使其唯一，或把 expected_count 设为实际次数"
        )
    updated = text.replace(old_text, new_text)
    # 落盘走同一条原子写路径：临时文件 + 替换前重新校验 baseline + 备份 + 保留权限位。
    # 这里只负责算出新内容，不再复制一份写文件的逻辑。
    written = _write_text_file_sync(
        path,
        content=updated,
        baseline_sha256=baseline_sha256,
        create_parents=False,
        max_bytes=max_bytes,
        backup_versions=backup_versions,
    )
    return TextFileReplaceResult(
        path=written.path,
        sha256=written.sha256,
        size_bytes=written.size_bytes,
        backup_path=written.backup_path,
        replacements=found,
    )


async def write_text_file(
    path: Path,
    *,
    content: str,
    baseline_sha256: str | None,
    create_parents: bool = False,
    settings: Settings,
) -> TextFileWriteResult:
    return await asyncio.to_thread(
        _write_text_file_sync,
        path,
        content=content,
        baseline_sha256=baseline_sha256,
        create_parents=create_parents,
        max_bytes=settings.cowork_file_write_max_bytes,
        backup_versions=settings.workspace_backup_versions_per_file,
    )


def _write_text_file_sync(
    path: Path,
    *,
    content: str,
    baseline_sha256: str | None,
    create_parents: bool,
    max_bytes: int,
    backup_versions: int,
) -> TextFileWriteResult:
    if path.suffix.casefold() in _KNOWN_BINARY_SUFFIXES:
        raise CoworkFileError(f"通用文本工具不能写入 {path.suffix} 二进制文档，请使用对应专用工具")
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        raise CoworkFileError(f"写入内容 {len(encoded)} bytes 超过上限 {max_bytes} bytes")
    created = not path.exists()
    if created and baseline_sha256 is not None:
        raise CoworkFileError("目标文件尚不存在，baseline_sha256 必须省略")
    parent = path.parent
    if not parent.is_dir():
        if not create_parents:
            raise CoworkFileError("目标文件的父目录不存在；需要创建时请设置 create_parents=true")
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise CoworkFileError(f"无法创建目标文件的父目录：{error}") from error
        if not parent.is_dir():  # pragma: no cover - 并发文件系统变化
            raise CoworkFileError("目标文件的父路径不是目录")
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
