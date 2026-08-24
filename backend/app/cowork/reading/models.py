"""工作区阅读引擎的数据模型：locator、unit、大纲、命中、引文校验。

核心抽象是 **locator**：对一份材料的 1-based 寻址。PDF 里它是物理页码，纯文本与
Markdown 里它是切出来的 section 序号。所有工具、提示词和（后续的）阅读器面板只说
locator，于是"模型引用第 12 页"和"阅读器滚到第 12 页"两件事不需要任何一方知道文件
到底是什么格式。

与 `app/rag` 的关系：**没有关系**。阅读引擎读的是工作区里的原始文件，走
`app/ingest/` 的共享解析器，不入库、不切 chunk、不做向量化。资料库跨文档检索仍然是
`search_knowledge` 的职责，两条路径故意不共用存储。

溯源口径沿用约束 3：unit 不是自由文本，而是一组 `ParsedBlock`。引文校验命中哪个
block，就把那个 block 的 `locations`（页码 / 归一化 bbox / 页面尺寸 / 旋转 /
坐标原点）原样交出去——高亮几何是解析的产物，不是前端猜出来的。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.ingest.types import ParsedBlock

# 一个 locator 指的是什么。纯展示用途（"第 12 页" vs "第 12 节"），寻址方式完全相同。
UnitKind = Literal["page", "section"]


class ReadingError(RuntimeError):
    """阅读操作失败，且失败原因需要让模型看见。

    按约束 4，message 是写给 LLM 的可执行指令而不是 stack trace：调用方直接把它
    作为失败的工具结果返回，模型据此改参数重试，而不是让整个 run 死掉。
    """


@dataclass(frozen=True)
class OutlineEntry:
    """大纲的一行。

    `level` 是 1-based 嵌套深度。`synthesised=True` 表示这一行不是文档自带的结构，
    而是用 unit 首行凑出来的——模型据此知道这个标题只能当线索、不能当章节名引用。
    """

    locator: int
    title: str
    level: int = 1
    synthesised: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "locator": self.locator,
            "title": self.title,
            "level": self.level,
            "synthesised": self.synthesised,
        }


@dataclass(frozen=True)
class SearchHit:
    """一条命中，带 locator 与原文窗口。"""

    locator: int
    snippet: str
    offset: int
    match: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "locator": self.locator,
            "snippet": self.snippet,
            "offset": self.offset,
            "match": self.match,
        }


@dataclass(frozen=True)
class ReadingUnit:
    """一个 locator 对应的内容。

    同时持有拼好的 `text`（给模型读）和构成它的 `blocks`（给引文校验定位几何）。
    两者必须来自同一次解析，否则高亮会落在错的地方——这就是为什么 unit 不接受
    外部传入的纯文本。
    """

    locator: int
    text: str
    blocks: tuple[ParsedBlock, ...]

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@dataclass(frozen=True)
class Material:
    """一份打开的工作区材料：解析一次，之后只按 locator 访问。"""

    path: Path
    material_id: str
    filename: str
    title: str
    unit: UnitKind
    units: tuple[ReadingUnit, ...]
    outline: tuple[OutlineEntry, ...]
    parser: str
    byte_size: int

    @property
    def unit_count(self) -> int:
        return len(self.units)

    @property
    def char_count(self) -> int:
        return sum(len(unit.text) for unit in self.units)

    def unit_at(self, locator: int) -> ReadingUnit:
        """按 locator 取 unit；越界时按约束 4 告诉模型合法区间。"""
        if not 1 <= locator <= self.unit_count:
            raise ReadingError(
                f"locator {locator} 越界：{self.filename} 只有 1..{self.unit_count}，"
                f"先调用 material_outline 看结构再取。"
            )
        return self.units[locator - 1]

    def iter_units(self) -> list[tuple[int, str]]:
        """给纯函数匹配器用的 `(locator, text)` 序列。"""
        return [(unit.locator, unit.text) for unit in self.units]

    def summary(self) -> str:
        size_kb = max(1, self.byte_size // 1024)
        label = "页" if self.unit == "page" else "节"
        return (
            f"{self.filename}（{self.unit_count} {label}，{self.char_count} 字符，"
            f"{size_kb} KB，解析器 {self.parser}）"
        )


@dataclass(frozen=True)
class QuoteCheck:
    """一句引文是不是真的出现在它声称的 locator 上。

    `blocks` 只在校验通过时非空，装的是命中引文的那些 block——`reader_goto` 直接
    把它们的 `locations` 交给前端画高亮，不需要前端再去文本层里模糊匹配一次。
    """

    verified: bool
    locator: int
    quote: str
    found_locator: int | None = None
    blocks: tuple[ParsedBlock, ...] = ()

    @property
    def moved(self) -> bool:
        """引文存在，但不在模型声称的那一页。"""
        return self.found_locator is not None and self.found_locator != self.locator


def block_locations(blocks: tuple[ParsedBlock, ...]) -> list[dict[str, Any]]:
    """把 block 的位置摊平成前端可直接渲染的行。

    字段与 `parsed_block_locations` 一一对应（约束 3）：只有 bbox 四个数不够，
    换个渲染器就会高亮错位，所以页面尺寸、旋转和坐标原点必须一起过河。
    """
    rows: list[dict[str, Any]] = []
    for block in blocks:
        for location in block.locations:
            rows.append(
                {
                    "page_no": location.page_no,
                    "page_width": location.page_width,
                    "page_height": location.page_height,
                    "rotation": location.rotation,
                    "coord_origin": location.coord_origin,
                    "bbox_norm": list(location.bbox_norm),
                }
            )
    return rows


__all__ = [
    "Material",
    "OutlineEntry",
    "QuoteCheck",
    "ReadingError",
    "ReadingUnit",
    "SearchHit",
    "UnitKind",
    "block_locations",
]
