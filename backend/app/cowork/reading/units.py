"""把一次解析的结果切成 locator 空间，并给出大纲。

只做一件事：`ParsedDocument` → `ReadingUnit` 序列 + `OutlineEntry` 序列。它是唯一
知道"PDF 按页切、纯文本按节切"的地方；service、search、工具层都只消费 unit，因此
以后新增格式只是这里多一个分支，下游一行不用改。

**为什么 PDF 要按物理页而不是按块数或字符数切**：页码是唯一同时对得上"用户在阅读器
里看到的东西"和"模型引用的东西"的单位。按字符切出来的第 7 段，用户在 PDF 上找不到。

**为什么空页也要占一个 locator**：一页只有图、没有文字层的论文很常见。跳过它会让此
后所有页码整体偏移一位——模型引第 12 页，用户翻到的是第 13 页，而且没有任何迹象表明
出了错。这是这一层最容易犯且最难发现的 bug。
"""

from __future__ import annotations

import re

from app.cowork.reading.models import OutlineEntry, ReadingUnit
from app.ingest.text_metrics import weighted_len
from app.ingest.types import ParsedBlock, ParsedDocument

# 一节的目标长度，单位是**加权字符**而不是裸字符。取得接近一页密排正文的分量，让
# "一节"对模型和对用户的体量感都跟"一页"差不多。
SECTION_TARGET_CHARS = 2_800
# Markdown 标题的井号前后缀，用于在没有 heading_path 时兜底剥掉标记。
_MD_MARKER = re.compile(r"^\s{0,3}#{1,6}\s*|\s*#*\s*$")
# 缓冲区已经攒到这个比例后，遇到标题就提前断开。没有这个下限的话，连续的多级标题
# 会切出一串只有一行的 section，大纲比正文还长。
SECTION_HEADING_FLUSH_RATIO = 0.5
# 大纲一次最多渲染多少行。九百页的书整份大纲本身就会撑爆提示词。
MAX_OUTLINE_ROWS = 120
# 用 unit 首行凑标题时的长度上限。
LABEL_CHARS = 90


def units_from_pages(document: ParsedDocument) -> tuple[ReadingUnit, ...]:
    """按物理页切 PDF：locator == page_no。"""
    page_count = document.page_count or _max_page(document.blocks)
    if page_count <= 0:
        return ()

    buckets: dict[int, list[ParsedBlock]] = {}
    # 没有 location 的块（解析器降级时可能出现）跟随上一个有页码的块，而不是被丢掉。
    last_page = 1
    for block in document.blocks:
        page = block.locations[0].page_no if block.locations else last_page
        last_page = page
        if 1 <= page <= page_count:
            buckets.setdefault(page, []).append(block)

    return tuple(
        _unit(locator, tuple(buckets.get(locator, ()))) for locator in range(1, page_count + 1)
    )


def units_from_sections(document: ParsedDocument) -> tuple[ReadingUnit, ...]:
    """按块边界攒出定长的 section，优先在标题处断开。

    块整体进出，永不从中间劈开：block 是溯源锚点（约束 8 的同一口径），把它切两半
    等于把 `char_start/char_end` 区间作废。超长单块自己成一节，长度由
    `read_material` 的字符上限兜底。
    """
    units: list[ReadingUnit] = []
    buffer: list[ParsedBlock] = []
    size = 0.0

    def flush() -> None:
        nonlocal size
        if buffer:
            units.append(_unit(len(units) + 1, tuple(buffer)))
            buffer.clear()
            size = 0.0

    for block in document.blocks:
        starts_section = (
            block.block_type == "title"
            and size >= SECTION_TARGET_CHARS * SECTION_HEADING_FLUSH_RATIO
        )
        if starts_section:
            flush()
        buffer.append(block)
        size += weighted_len(block.text)
        if size >= SECTION_TARGET_CHARS:
            flush()
    flush()
    return tuple(units)


def build_outline(units: tuple[ReadingUnit, ...]) -> tuple[OutlineEntry, ...]:
    """优先用文档自带的结构，没有就用 unit 首行凑。

    自带结构指 `block_type == "title"` 的块——Markdown 解析器和 MinerU 都会标出来。
    PyMuPDF 路径把所有块都标成 paragraph，所以纯文本层 PDF 会落到凑标题这一支；
    调用方可以另外用 PDF 书签补一份更好的（见 `materials.py`）。
    """
    structural = _structural_outline(units)
    if structural:
        return structural
    return tuple(
        OutlineEntry(
            locator=unit.locator, title=first_line_label(unit.text), level=1, synthesised=True
        )
        for unit in units
    )


def _structural_outline(units: tuple[ReadingUnit, ...]) -> tuple[OutlineEntry, ...]:
    entries: list[OutlineEntry] = []
    for unit in units:
        for block in unit.blocks:
            if block.block_type != "title":
                continue
            title = heading_text(block)
            if not title:
                continue
            # heading_path 的长度就是嵌套深度：Markdown 与 MinerU 都按层级维护它。
            level = max(1, len(block.heading_path) or 1)
            entries.append(
                OutlineEntry(locator=unit.locator, title=_clip(title, LABEL_CHARS), level=level)
            )
    return tuple(entries)


def trim_outline(entries: tuple[OutlineEntry, ...]) -> tuple[tuple[OutlineEntry, ...], int]:
    """把大纲压到上限内，必须砍时优先保留浅层。

    先砍最深的层级而不是直接截前 N 行：截断会把模型永远困在第一章，而砍深度至少还
    保留了整份文档的形状。
    """
    if len(entries) <= MAX_OUTLINE_ROWS:
        return entries, 0
    for max_level in range(1, 7):
        kept = tuple(entry for entry in entries if entry.level <= max_level)
        if len(kept) > MAX_OUTLINE_ROWS:
            shallower = tuple(entry for entry in entries if entry.level < max_level)
            if shallower:
                capped = shallower[:MAX_OUTLINE_ROWS]
                return capped, len(entries) - len(capped)
            break
    return entries[:MAX_OUTLINE_ROWS], len(entries) - MAX_OUTLINE_ROWS


def heading_text(block: ParsedBlock) -> str:
    """标题块的干净文字。

    优先取 `heading_path` 的最后一段：Markdown 与 MinerU 都往里放已经去掉标记的标题，
    而 `block.text` 保留的是原样的 `## 1 引言`。直接用 block.text 会把井号一路带进
    大纲和引用里。取不到时退回自己剥一次标记。
    """
    if block.heading_path:
        cleaned = " ".join(block.heading_path[-1].split())
        if cleaned:
            return _clip(cleaned, LABEL_CHARS)
    stripped = _MD_MARKER.sub("", " ".join(block.text.split())).strip()
    return _clip(stripped, LABEL_CHARS)


def first_line_label(text: str, *, limit: int = LABEL_CHARS) -> str:
    """一个 unit 的短标签：第一行有内容的文字。"""
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if len(line) < 2:
            continue
        return _clip(line, limit)
    return ""


# unit.text 里 block 之间的分隔符。`_unit` 与 `block_spans` 都依赖它，改一处必须
# 改另一处，否则引文能校验通过却高亮到错误的块上。
_BLOCK_JOINER = "\n\n"


def _unit(locator: int, blocks: tuple[ParsedBlock, ...]) -> ReadingUnit:
    return ReadingUnit(
        locator=locator,
        text=_BLOCK_JOINER.join(block.text for block in blocks if block.text.strip()),
        blocks=blocks,
    )


def block_spans(unit: ReadingUnit) -> list[tuple[int, int, ParsedBlock]]:
    """每个 block 在 `unit.text` 里占的字符区间。

    引文校验拿到偏移之后靠它反查是哪个块，从而拿到 bbox。拼接规则与 `_unit` 共用
    同一个分隔符常量，正是为了让这两件事不会各自演化。
    """
    spans: list[tuple[int, int, ParsedBlock]] = []
    cursor = 0
    for block in unit.blocks:
        if not block.text.strip():
            continue
        if spans:
            cursor += len(_BLOCK_JOINER)
        spans.append((cursor, cursor + len(block.text), block))
        cursor += len(block.text)
    return spans


def _max_page(blocks: list[ParsedBlock]) -> int:
    pages = [location.page_no for block in blocks for location in block.locations]
    return max(pages) if pages else 0


def _clip(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


__all__ = [
    "MAX_OUTLINE_ROWS",
    "SECTION_TARGET_CHARS",
    "block_spans",
    "build_outline",
    "first_line_label",
    "heading_text",
    "trim_outline",
    "units_from_pages",
    "units_from_sections",
]
