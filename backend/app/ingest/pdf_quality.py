import math
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass

from app.ingest.types import ParsedDocument


@dataclass(frozen=True)
class PdfSourceAnalysis:
    image_count: int = 0
    multi_column_pages: int = 0
    pages_with_text: int = 0


@dataclass(frozen=True)
class PdfQualityMetrics:
    page_count: int
    block_count: int
    character_count: int
    characters_per_page: float
    located_block_ratio: float
    replacement_character_ratio: float
    control_character_ratio: float
    block_type_counts: dict[str, int]
    image_count: int
    multi_column_pages: int
    pages_with_text: int
    issues: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class PdfQualityError(ValueError):
    pass


def assess_pdf_quality(
    document: ParsedDocument,
    source: PdfSourceAnalysis | None = None,
) -> PdfQualityMetrics:
    source = source or PdfSourceAnalysis()
    page_count = document.page_count or 0
    character_count = len(document.full_text)
    replacement_count = document.full_text.count("\ufffd")
    control_count = sum(
        unicodedata.category(character) == "Cc" and character not in "\n\t"
        for character in document.full_text
    )
    located = sum(bool(block.locations) for block in document.blocks)
    block_types = Counter(block.block_type for block in document.blocks)
    issues: list[str] = []
    if not document.blocks or not document.full_text.strip():
        issues.append("empty_text")
    characters_per_page = character_count / page_count if page_count else 0.0
    if page_count and characters_per_page < 40:
        issues.append("low_text_density")
    replacement_ratio = replacement_count / max(1, character_count)
    if replacement_ratio > 0.002:
        issues.append("replacement_characters")
    control_ratio = control_count / max(1, character_count)
    if control_ratio > 0.001:
        issues.append("control_characters")
    located_ratio = located / max(1, len(document.blocks))
    if located_ratio < 1.0:
        issues.append("missing_locations")

    return PdfQualityMetrics(
        page_count=page_count,
        block_count=len(document.blocks),
        character_count=character_count,
        characters_per_page=characters_per_page,
        located_block_ratio=located_ratio,
        replacement_character_ratio=replacement_ratio,
        control_character_ratio=control_ratio,
        block_type_counts=dict(sorted(block_types.items())),
        image_count=source.image_count,
        multi_column_pages=source.multi_column_pages,
        pages_with_text=source.pages_with_text,
        issues=tuple(issues),
    )


def validate_pdf_document(document: ParsedDocument) -> None:
    if document.page_count is None or document.page_count < 1:
        raise PdfQualityError("PDF page_count 无效")
    if not document.blocks or not document.full_text.strip():
        raise PdfQualityError("PDF 解析结果为空")
    previous_end = -1
    for expected_index, block in enumerate(document.blocks):
        if block.block_idx != expected_index:
            raise PdfQualityError("PDF block_idx 不连续")
        if block.char_start < 0 or block.char_end <= block.char_start:
            raise PdfQualityError("PDF block 字符区间无效")
        if block.char_start <= previous_end:
            raise PdfQualityError("PDF block 字符区间重叠或逆序")
        if document.full_text[block.char_start : block.char_end] != block.text:
            raise PdfQualityError("PDF block 字符区间无法回切原文")
        if not block.locations:
            raise PdfQualityError("PDF block 缺少页码/bbox 定位")
        for location in block.locations:
            if not 1 <= location.page_no <= document.page_count:
                raise PdfQualityError("PDF block 页码越界")
            if location.page_width <= 0 or location.page_height <= 0:
                raise PdfQualityError("PDF 页面尺寸无效")
            if location.rotation not in {0, 90, 180, 270}:
                raise PdfQualityError("PDF 页面旋转角无效")
            if location.coord_origin != "top_left":
                raise PdfQualityError("PDF bbox 必须使用 top_left 原点")
            x0, y0, x1, y1 = location.bbox_norm
            if not all(math.isfinite(value) and 0 <= value <= 1 for value in location.bbox_norm):
                raise PdfQualityError("PDF bbox 必须为 [0,1] 有限数")
            if x0 >= x1 or y0 >= y1:
                raise PdfQualityError("PDF bbox 矩形无效")
        previous_end = block.char_end


def should_prefer_mineru(metrics: PdfQualityMetrics) -> tuple[bool, tuple[str, ...]]:
    reasons = list(metrics.issues)
    if metrics.multi_column_pages:
        reasons.append("multi_column_layout")
    if metrics.image_count:
        reasons.append("embedded_images")
    return bool(reasons), tuple(dict.fromkeys(reasons))
