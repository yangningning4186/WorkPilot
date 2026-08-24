"""工作区沉浸阅读引擎——材料、locator、大纲、引文校验。

一份**材料**就是工作区里一个用户在读的文件：它被切一次成 **unit**，此后所有操作都
按 **locator**（1-based 的页 / 节序号）寻址。就是这一个抽象，让模型能引"第 12 页"、
阅读器能滚到第 12 页，而两边都不需要知道文件是 PDF 还是 Markdown。

自底向上分层，每层只依赖列表里排在它上面的：

* :mod:`.models`   —— dataclass 与错误类型。无 I/O，不 import 任何兄弟模块。
* :mod:`.search`   —— `(locator, text)` 上的纯匹配函数。
* :mod:`.units`    —— 解析结果 → unit 与大纲。唯一知道格式差异的地方。
* :mod:`.materials`—— 路径 → 材料，带进程内解析缓存。
* :mod:`.service`  —— 调用方真正使用的组合层。

这里不 import Cowork 的工具注册表、不 import FastAPI，也不 import `app.rag`（ADR-0011
要求两个产品包互不依赖，而阅读引擎本来就不需要向量检索）。
"""

from app.cowork.reading.materials import MaterialCache, default_material_cache
from app.cowork.reading.models import (
    Material,
    OutlineEntry,
    QuoteCheck,
    ReadingError,
    ReadingUnit,
    SearchHit,
    UnitKind,
    block_locations,
)
from app.cowork.reading.search import SearchResult, locate_quote, normalise, search_units, terms_of
from app.cowork.reading.service import (
    LOCATE_HITS,
    MAX_LOCATORS_PER_READ,
    RenderedUnits,
    parse_locators,
    render_locate_block,
    render_outline,
    render_units,
    search_material,
    unit_label,
    verify_quote,
)
from app.cowork.reading.units import units_from_pages, units_from_sections

__all__ = [
    "LOCATE_HITS",
    "MAX_LOCATORS_PER_READ",
    "Material",
    "MaterialCache",
    "OutlineEntry",
    "QuoteCheck",
    "ReadingError",
    "ReadingUnit",
    "RenderedUnits",
    "SearchHit",
    "SearchResult",
    "UnitKind",
    "block_locations",
    "default_material_cache",
    "locate_quote",
    "normalise",
    "parse_locators",
    "render_locate_block",
    "render_outline",
    "render_units",
    "search_material",
    "search_units",
    "terms_of",
    "unit_label",
    "units_from_pages",
    "units_from_sections",
    "verify_quote",
]
