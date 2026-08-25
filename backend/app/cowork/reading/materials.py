"""把工作区里的一个文件变成可按 locator 访问的材料，并缓存解析结果。

**为什么要缓存**：一次"找到再读再定位"至少三次工具调用，而一份 40 页论文用 PyMuPDF
解析要一到几秒、MinerU 更久。不缓存的话每次调用都重解析一遍同一个文件，模型的每一
步都要等，而且 CPU 全花在重复劳动上。

**为什么缓存只在进程内、不落盘**：落盘缓存要自己解决淘汰、并发写和半截文件——阅读
引擎不该为了省一次解析引入第二套需要原子写和文件锁的存储。worker 进程是长驻的，
一次会话里的连续工具调用都会落在同一个进程上，进程内缓存已经吃掉了绝大部分收益。

**缓存键是 (规范路径, mtime_ns, size) 而不是内容哈希**：哈希要把整个文件读一遍，
而这个判断每次工具调用都要做。stat 三元组不匹配就重新解析并重算哈希，所以用户改了
文件之后拿到的一定是新解析——不会出现模型引用一份已经被编辑掉的旧内容。
"""

from __future__ import annotations

import asyncio
import hashlib
import stat as stat_module
from collections import OrderedDict
from functools import partial
from pathlib import Path
from typing import Any

import structlog

from app.core.config import Settings
from app.cowork.reading.models import Material, OutlineEntry, ReadingError, ReadingUnit
from app.cowork.reading.units import (
    build_outline,
    heading_text,
    units_from_pages,
    units_from_sections,
)
from app.ingest.markdown import parse_markdown
from app.ingest.pdf import PdfParseError, parse_pdf
from app.ingest.settings import pdf_parser_config_from_settings
from app.ingest.types import ParsedDocument

logger = structlog.get_logger(__name__)

# 同时缓存几份材料。阅读场景里用户手上通常就一两篇论文；开得太大只是让一份被遗忘的
# 六百页 PDF 一直占着内存。
MAX_CACHED_MATERIALS = 6

# 走 Markdown 解析器的后缀。其余纯文本（.txt、代码）也用同一个解析器：标题正则匹配
# 不上就退化成按段落切块，char offset 口径不变，不值得为此再写一个解析器。
_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown", ".mdx"})
_MATERIAL_ID_CHARS = 16

# stat 三元组：设备无关，够用来判断"还是同一份内容吗"。
_CacheKey = tuple[str, int, int]


class MaterialCache:
    """进程内的材料缓存，按 stat 三元组失效。"""

    def __init__(self, *, max_entries: int = MAX_CACHED_MATERIALS) -> None:
        if max_entries < 1:
            raise ValueError("材料缓存容量必须大于 0")
        self._entries: OrderedDict[_CacheKey, Material] = OrderedDict()
        self._inflight: dict[_CacheKey, asyncio.Task[Material]] = {}
        self._max_entries = max_entries

    async def load(self, path: Path, *, settings: Settings) -> Material:
        """取（必要时解析）一份材料。

        同一个键上串行：一轮里并发发起的 `search_material` 和 `read_material` 否则
        会各自解析一遍同一个 PDF，付两次钱等两次。
        """
        key = await asyncio.to_thread(_cache_key, path, settings)
        cached = self._entries.get(key)
        if cached is not None:
            self._entries.move_to_end(key)
            return cached

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                self._build_and_cache(key, path, settings),
                name=f"material-cache-{path.name}",
            )
            self._inflight[key] = task
            task.add_done_callback(partial(self._finish, key))

        # 一个 HTTP/run 请求被取消，不应该顺手取消其他等待者共用的 PDF 解析。解析继续完成
        # 后还会进入缓存；下一次读取可以直接命中，而不是再付一次解析成本。
        return await asyncio.shield(task)

    async def _build_and_cache(
        self,
        key: _CacheKey,
        path: Path,
        settings: Settings,
    ) -> Material:
        material = await _build_material(path, settings=settings, byte_size=key[2])
        self._entries[key] = material
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return material

    def _finish(self, key: _CacheKey, task: asyncio.Task[Material]) -> None:
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)
        # 如果唯一的等待者先被取消，失败的后台解析就没有人读取异常。显式取一次，避免
        # asyncio 在 worker 日志里产生 "Task exception was never retrieved" 噪声。
        if not task.cancelled():
            task.exception()


def _cache_key(path: Path, settings: Settings) -> _CacheKey:
    """校验文件可读并算出缓存键。运行在线程里，因为 stat 是阻塞调用。"""
    try:
        info = path.stat()
    except OSError as error:
        raise ReadingError(
            f"打不开 {path}：文件不存在或没有读取权限。先用 list_files 确认路径。"
        ) from error
    if not stat_module.S_ISREG(info.st_mode):
        raise ReadingError(f"{path} 不是普通文件，阅读工具只能打开文件，不能打开目录。")
    limit = _size_limit(path, settings)
    if info.st_size > limit:
        raise ReadingError(
            f"{path.name} 有 {info.st_size} 字节，超过阅读上限 {limit} 字节，无法整篇打开。"
        )
    return (str(path), info.st_mtime_ns, info.st_size)


def _size_limit(path: Path, settings: Settings) -> int:
    if path.suffix.casefold() == ".pdf":
        return settings.pdf_max_bytes
    return settings.cowork_file_read_max_bytes


async def _build_material(path: Path, *, settings: Settings, byte_size: int) -> Material:
    if path.suffix.casefold() == ".pdf":
        return await _build_pdf(path, settings=settings, byte_size=byte_size)
    return await _build_text(path, settings=settings, byte_size=byte_size)


async def _build_pdf(path: Path, *, settings: Settings, byte_size: int) -> Material:
    try:
        parsed = await parse_pdf(path, pdf_parser_config_from_settings(settings))
    except PdfParseError as error:
        raise ReadingError(
            f"{path.name} 解析失败：{error}。扫描件需要先做 OCR 才能按页阅读。"
        ) from error

    units = units_from_pages(parsed.document)
    if not units:
        raise ReadingError(f"{path.name} 没有可读的页面，可能是空文件或纯图片扫描件。")
    if not any(not unit.is_empty for unit in units):
        raise ReadingError(
            f"{path.name} 每一页都抽不出文字，应该是没有文本层的扫描件；"
            "先做 OCR，或改用 read_file 查看解析质量报告。"
        )

    outline = await asyncio.to_thread(_pdf_bookmarks, path, len(units)) or build_outline(units)
    return Material(
        path=path,
        material_id=await asyncio.to_thread(_content_hash, path),
        filename=path.name,
        title=parsed.title or path.stem,
        unit="page",
        units=units,
        outline=outline,
        parser=parsed.parser,
        byte_size=byte_size,
    )


async def _build_text(path: Path, *, settings: Settings, byte_size: int) -> Material:
    raw = await asyncio.to_thread(_read_text, path, settings)
    try:
        document: ParsedDocument = await asyncio.to_thread(parse_markdown, raw)
    except ValueError as error:
        raise ReadingError(f"{path.name} 没有可读内容：{error}") from error

    units = units_from_sections(document)
    if not units:
        raise ReadingError(f"{path.name} 没有可读内容。")
    return Material(
        path=path,
        material_id=await asyncio.to_thread(_content_hash, path),
        filename=path.name,
        title=_document_title(units) or path.stem,
        unit="section",
        units=units,
        outline=build_outline(units),
        parser="markdown" if path.suffix.casefold() in _MARKDOWN_SUFFIXES else "text",
        byte_size=byte_size,
    )


def _read_text(path: Path, settings: Settings) -> str:
    limit = settings.cowork_file_read_max_bytes
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ReadingError(f"{path.name} 在读取期间超过上限 {limit} 字节。")
    if b"\x00" in content:
        raise ReadingError(
            f"{path.name} 看起来是二进制文件，不能按文本阅读。PDF 请直接传 .pdf 路径。"
        )
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ReadingError(f"{path.name} 不是 UTF-8 文本，无法阅读。") from error


def _pdf_bookmarks(path: Path, unit_count: int) -> tuple[OutlineEntry, ...]:
    """从 PDF 书签树取大纲。

    单独开一次文件只读目录树，不渲染也不抽文字，所以不需要走解析子进程的资源上限
    （那条约束防的是 MinerU 遇畸形 PDF 时的 OOM）。取不到就返回空，调用方回退到用
    unit 首行凑——PyMuPDF 路径把所有块都标成 paragraph，论文的章节结构基本只能从
    书签拿到，值得为此多开一次文件。

    指向页码范围之外的书签直接丢掉而不是夹逼：把"第 900 页"改成最后一页，会让模型
    引用到用户根本没问的内容。
    """
    try:
        import pymupdf

        document: Any = pymupdf.open(path)  # type: ignore[no-untyped-call]
        try:
            toc = document.get_toc()
        finally:
            document.close()
    except Exception:
        logger.debug("reading.pdf.no_bookmarks", path=str(path), exc_info=True)
        return ()

    entries: list[OutlineEntry] = []
    for row in toc or []:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        try:
            level, title, page = int(row[0]), str(row[1]).strip(), int(row[2])
        except (TypeError, ValueError):
            continue
        if not title or not 1 <= page <= unit_count:
            continue
        entries.append(OutlineEntry(locator=page, title=title, level=max(1, level)))
    return tuple(entries)


def _document_title(units: tuple[ReadingUnit, ...]) -> str:
    for block in units[0].blocks if units else ():
        if block.block_type == "title":
            return heading_text(block)
    return ""


def _content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:_MATERIAL_ID_CHARS]


_DEFAULT_CACHE = MaterialCache()


def default_material_cache() -> MaterialCache:
    """进程级共享的那一份缓存。

    工具和 locate 预检索必须打同一份，否则同一轮里同一个文件会被解析两次。跨会话共享是
    安全的、而且是想要的：缓存键含 stat 三元组，而**能不能读**由调用方在进入这里之前
    完成的目录授权决定——缓存本身不是边界，它坐在边界后面。
    """
    return _DEFAULT_CACHE


__all__ = [
    "MAX_CACHED_MATERIALS",
    "MaterialCache",
    "default_material_cache",
]
