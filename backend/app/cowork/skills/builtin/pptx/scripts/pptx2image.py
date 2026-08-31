"""内置 pptx Skill 携带的确定性逐页栅格化脚本。

This is deliberately narrower than PowerPoint.  It renders the shape subset emitted by
WorkPilot's presentation renderer and rejects unsupported objects instead of claiming
that visual QA completed.  The source of truth is the saved PPTX, not the input Spec.
"""

from __future__ import annotations

import io
import math
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from unicodedata import category

import pymupdf
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE, MSO_SHAPE_TYPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn

_pymupdf: Any = pymupdf


class PptxRasterError(RuntimeError):
    """The presentation cannot be rendered faithfully by the bundled renderer."""


@dataclass(frozen=True)
class PptxRasterResult:
    pages: tuple[Path, ...]
    overflow_shapes: tuple[str, ...]
    unsupported_shapes: tuple[str, ...]


@dataclass(frozen=True)
class _ResolvedFont:
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    stroke_width: int = 0


_REGULAR_FONTS = (
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    Path("/System/Library/Fonts/Supplemental/Helvetica.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/arial.ttf"),
)
_BOLD_FONTS = (
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("C:/Windows/Fonts/msyhbd.ttc"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
)
_SUPPORTED_AUTO_SHAPES = frozenset(
    {
        MSO_AUTO_SHAPE_TYPE.RECTANGLE,
        MSO_AUTO_SHAPE_TYPE.ROUNDED_RECTANGLE,
        MSO_AUTO_SHAPE_TYPE.OVAL,
        MSO_AUTO_SHAPE_TYPE.CHEVRON,
        MSO_AUTO_SHAPE_TYPE.HEXAGON,
        MSO_AUTO_SHAPE_TYPE.TRAPEZOID,
    }
)


def render_presentation_pages(
    source: Path,
    output_dir: Path,
    *,
    width_px: int = 1600,
) -> PptxRasterResult:
    """Render every slide to PNG and report fidelity/overflow failures."""

    if source.suffix.casefold() != ".pptx":
        raise ValueError("PPTX rasterizer 只支持 .pptx")
    if width_px < 800 or width_px > 4096:
        raise ValueError("PPTX rasterizer width_px 必须在 800–4096 之间")
    presentation = Presentation(str(source))
    slide_width = int(presentation.slide_width or 0)
    slide_height = int(presentation.slide_height or 0)
    if slide_width <= 0 or slide_height <= 0:
        raise PptxRasterError("PPTX 页面尺寸无效")
    height_px = max(1, round(width_px * slide_height / slide_width))
    output_dir.mkdir(parents=True, exist_ok=True)
    overflow: list[str] = []
    unsupported: list[str] = []
    pages: list[Path] = []
    for slide_index, slide in enumerate(presentation.slides, start=1):
        background = _slide_background(slide)
        canvas = Image.new("RGB", (width_px, height_px), background)
        draw = ImageDraw.Draw(canvas)
        for shape_index, shape in enumerate(slide.shapes, start=1):
            label = f"第 {slide_index} 页对象 {shape_index}"
            try:
                handled, text_overflow = _render_shape(
                    canvas,
                    draw,
                    shape,
                    slide_width=slide_width,
                    slide_height=slide_height,
                )
            except (OSError, ValueError, TypeError, AttributeError) as error:
                raise PptxRasterError(f"{label} 渲染失败：{error}") from error
            if not handled:
                unsupported.append(f"{label}（{getattr(shape, 'shape_type', 'unknown')}）")
            if text_overflow:
                overflow.append(label)
        page = output_dir / f"slide-{slide_index:03d}.png"
        canvas.save(page, format="PNG", optimize=True)
        pages.append(page)
    if not pages:
        raise PptxRasterError("PPTX 没有可渲染页面")
    return PptxRasterResult(
        pages=tuple(pages),
        overflow_shapes=tuple(overflow),
        unsupported_shapes=tuple(unsupported),
    )


def _slide_background(slide: Any) -> tuple[int, int, int]:
    return _fill_color(getattr(slide.background, "fill", None), (255, 255, 255)) or (
        255,
        255,
        255,
    )


def _render_shape(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    shape: Any,
    *,
    slide_width: int,
    slide_height: int,
) -> tuple[bool, bool]:
    box = _pixel_box(shape, slide_width, slide_height, canvas.width, canvas.height)
    shape_type = shape.shape_type
    if shape_type == MSO_SHAPE_TYPE.PICTURE:
        _draw_picture(canvas, shape, box)
        return True, False
    if shape_type == MSO_SHAPE_TYPE.CHART:
        return _draw_chart(draw, shape, box), False
    if shape_type == MSO_SHAPE_TYPE.LINE:
        _draw_connector(
            draw,
            shape,
            box,
            pixels_per_emu=canvas.width / slide_width,
        )
        return True, False
    if shape_type in {MSO_SHAPE_TYPE.AUTO_SHAPE, MSO_SHAPE_TYPE.TEXT_BOX}:
        if shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE:
            # PptxGenJS serializes native straight connectors as ``p:sp`` with
            # ``a:prstGeom prst="line"``. python-pptx therefore reports AUTO_SHAPE
            # instead of LINE and cannot map ``line`` through auto_shape_type.
            preset = str(shape._element.spPr.prstGeom.get("prst") or "")
            if preset == "line" or preset.startswith("bentConnector"):
                _draw_connector(
                    draw,
                    shape,
                    box,
                    pixels_per_emu=canvas.width / slide_width,
                )
                return True, False
            auto_type = shape.auto_shape_type
            if auto_type not in _SUPPORTED_AUTO_SHAPES:
                return False, False
            _draw_auto_shape(draw, shape, box, auto_type)
        if getattr(shape, "has_text_frame", False):
            return True, _draw_text_frame(draw, shape, box, canvas.width / slide_width)
        return True, False
    # Empty placeholders from a template are harmless; populated placeholders are not.
    if shape_type == MSO_SHAPE_TYPE.PLACEHOLDER and not str(getattr(shape, "text", "")).strip():
        return True, False
    return False, False


def _pixel_box(
    shape: Any,
    slide_width: int,
    slide_height: int,
    width_px: int,
    height_px: int,
) -> tuple[int, int, int, int]:
    left = round(int(shape.left or 0) * width_px / slide_width)
    top = round(int(shape.top or 0) * height_px / slide_height)
    right = round((int(shape.left or 0) + int(shape.width or 0)) * width_px / slide_width)
    bottom = round((int(shape.top or 0) + int(shape.height or 0)) * height_px / slide_height)
    return left, top, right, bottom


def _fill_color(fill: Any, default: tuple[int, int, int] | None) -> tuple[int, int, int] | None:
    if fill is None or getattr(fill, "type", None) is None:
        return default
    try:
        rgb = fill.fore_color.rgb
    except (AttributeError, ValueError, TypeError):
        return default
    if rgb is None:
        return default
    return int(rgb[0]), int(rgb[1]), int(rgb[2])


def _line_color(shape: Any) -> tuple[int, int, int] | None:
    try:
        rgb = shape.line.color.rgb
    except (AttributeError, ValueError, TypeError):
        return None
    if rgb is None:
        return None
    return int(rgb[0]), int(rgb[1]), int(rgb[2])


def _draw_auto_shape(
    draw: ImageDraw.ImageDraw,
    shape: Any,
    box: tuple[int, int, int, int],
    auto_type: Any,
) -> None:
    fill = _fill_color(shape.fill, None)
    outline = _line_color(shape)
    width = max(1, round(float(getattr(shape.line, "width", 0) or 0) / 914400 * 120))
    if auto_type == MSO_AUTO_SHAPE_TYPE.OVAL:
        draw.ellipse(box, fill=fill, outline=outline, width=width)
    elif auto_type == MSO_AUTO_SHAPE_TYPE.ROUNDED_RECTANGLE:
        radius = max(2, min(box[2] - box[0], box[3] - box[1]) // 12)
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)
    elif auto_type in {
        MSO_AUTO_SHAPE_TYPE.CHEVRON,
        MSO_AUTO_SHAPE_TYPE.HEXAGON,
        MSO_AUTO_SHAPE_TYPE.TRAPEZOID,
    }:
        left, top, right, bottom = box
        box_width = right - left
        box_height = bottom - top
        if auto_type == MSO_AUTO_SHAPE_TYPE.CHEVRON:
            inset = box_width * 0.22
            points = [
                (left, top),
                (right - inset, top),
                (right, top + box_height / 2),
                (right - inset, bottom),
                (left, bottom),
                (left + inset, top + box_height / 2),
            ]
        elif auto_type == MSO_AUTO_SHAPE_TYPE.HEXAGON:
            inset = box_width * 0.2
            points = [
                (left + inset, top),
                (right - inset, top),
                (right, top + box_height / 2),
                (right - inset, bottom),
                (left + inset, bottom),
                (left, top + box_height / 2),
            ]
        else:
            inset = box_width * 0.14
            points = [
                (left + inset, top),
                (right - inset, top),
                (right, bottom),
                (left, bottom),
            ]
        draw.polygon(points, fill=fill, outline=outline)
        if outline is not None and width > 1:
            draw.line([*points, points[0]], fill=outline, width=width, joint="curve")
    else:
        draw.rectangle(box, fill=fill, outline=outline, width=width)


def _draw_connector(
    draw: ImageDraw.ImageDraw,
    shape: Any,
    box: tuple[int, int, int, int],
    *,
    pixels_per_emu: float,
) -> None:
    left, top, right, bottom = box
    transform = shape._element.spPr.xfrm
    flip_h = transform.get("flipH") in {"1", "true"}
    flip_v = transform.get("flipV") in {"1", "true"}
    start = (right if flip_h else left, bottom if flip_v else top)
    end = (left if flip_h else right, top if flip_v else bottom)
    preset = str(shape._element.spPr.prstGeom.get("prst") or "line")
    if preset.startswith("bentConnector"):
        middle_x = round((start[0] + end[0]) / 2)
        points = [start, (middle_x, start[1]), (middle_x, end[1]), end]
    else:
        points = [start, end]
    color = _line_color(shape) or (95, 109, 102)
    width = max(2, round(float(getattr(shape.line, "width", 0) or 0) * pixels_per_emu))
    draw.line(points, fill=color, width=width, joint="curve")

    line = shape.line._ln
    tail = line.find(qn("a:tailEnd")) if line is not None else None
    if tail is None or tail.get("type") in {None, "none"}:
        return
    previous = points[-2]
    delta_x = end[0] - previous[0]
    delta_y = end[1] - previous[1]
    length = math.hypot(delta_x, delta_y)
    if length <= 0:
        return
    unit_x = delta_x / length
    unit_y = delta_y / length
    arrow_length = max(7, width * 4)
    half_width = arrow_length * 0.45
    base_x = end[0] - unit_x * arrow_length
    base_y = end[1] - unit_y * arrow_length
    perpendicular_x = -unit_y
    perpendicular_y = unit_x
    draw.polygon(
        [
            end,
            (
                round(base_x + perpendicular_x * half_width),
                round(base_y + perpendicular_y * half_width),
            ),
            (
                round(base_x - perpendicular_x * half_width),
                round(base_y - perpendicular_y * half_width),
            ),
        ],
        fill=color,
    )


def _draw_picture(canvas: Image.Image, shape: Any, box: tuple[int, int, int, int]) -> None:
    with Image.open(io.BytesIO(shape.image.blob)) as raw:
        image = raw.convert("RGBA")
        left_crop = round(image.width * float(shape.crop_left or 0))
        right_crop = round(image.width * (1 - float(shape.crop_right or 0)))
        top_crop = round(image.height * float(shape.crop_top or 0))
        bottom_crop = round(image.height * (1 - float(shape.crop_bottom or 0)))
        if right_crop <= left_crop or bottom_crop <= top_crop:
            raise ValueError("图片裁剪区域为空")
        image = image.crop((left_crop, top_crop, right_crop, bottom_crop))
        width = max(1, box[2] - box[0])
        height = max(1, box[3] - box[1])
        image = image.resize((width, height), Image.Resampling.LANCZOS)
        canvas.paste(image, (box[0], box[1]), image)


def _draw_chart(
    draw: ImageDraw.ImageDraw,
    shape: Any,
    box: tuple[int, int, int, int],
) -> bool:
    chart = shape.chart
    chart_type = str(getattr(chart, "chart_type", "")).upper()
    is_line = "LINE" in chart_type
    is_column = "COLUMN_CLUSTERED" in chart_type
    if not (is_line or is_column) or len(chart.plots) != 1:
        return False
    plot_object = chart.plots[0]
    if bool(getattr(chart, "has_title", False)):
        return False
    show_data_labels = bool(getattr(plot_object, "has_data_labels", False))
    left, top, right, bottom = box
    draw.rectangle(box, fill=(255, 255, 255), outline=(205, 215, 210), width=2)
    plot = (left + 82, top + 28, right - 25, bottom - 88)
    if plot[2] <= plot[0] or plot[3] <= plot[1]:
        raise ValueError("图表区域过小")
    series = list(chart.series)
    values = [tuple(float(value or 0) for value in item.values) for item in series]
    if not values or not any(values):
        return True
    categories = [
        str(getattr(category_item, "label", category_item))
        for category_item in plot_object.categories
    ]
    count = max(len(item) for item in values)
    if len(categories) != count:
        return False
    extrema = [value for item in values for value in item]
    minimum = min(0.0, min(extrema))
    maximum = max(0.0, max(extrema))
    span = maximum - minimum or 1.0
    axis_y = round(plot[3] - (0.0 - minimum) / span * (plot[3] - plot[1]))
    draw.line((plot[0], plot[1], plot[0], plot[3]), fill=(105, 116, 111), width=2)
    draw.line((plot[0], axis_y, plot[2], axis_y), fill=(105, 116, 111), width=2)
    palette = ((22, 122, 91), (66, 116, 166), (195, 118, 48), (122, 95, 170))
    value_font = _bundled_unicode_font(12)
    group_width = (plot[2] - plot[0]) / max(1, count)
    for series_index, item in enumerate(values):
        color = palette[series_index % len(palette)]
        points: list[tuple[int, int]] = []
        for value_index, value in enumerate(item):
            center = plot[0] + group_width * (value_index + 0.5)
            y = round(plot[3] - (value - minimum) / span * (plot[3] - plot[1]))
            if is_line:
                points.append((round(center), y))
            else:
                bar_width = max(3, group_width * 0.68 / max(1, len(values)))
                x = center - group_width * 0.34 + series_index * bar_width
                draw.rectangle(
                    (round(x), min(axis_y, y), round(x + bar_width - 1), max(axis_y, y)), fill=color
                )
                if show_data_labels:
                    draw.text(
                        (round(x), min(axis_y, y) - 15),
                        f"{value:g}",
                        fill=(55, 65, 61),
                        font=value_font,
                    )
        if is_line and len(points) >= 2:
            draw.line(points, fill=color, width=4, joint="curve")
            for point_index, point in enumerate(points):
                draw.ellipse((point[0] - 4, point[1] - 4, point[0] + 4, point[1] + 4), fill=color)
                if show_data_labels:
                    draw.text(
                        (point[0] + 5, point[1] - 16),
                        f"{values[series_index][point_index]:g}",
                        fill=(55, 65, 61),
                        font=value_font,
                    )
    labels = [*categories, *(str(getattr(item, "name", "")) for item in series)]
    font = _bundled_unicode_font(14)
    for index, label in enumerate(categories):
        center = round(plot[0] + group_width * (index + 0.5))
        shortened = label if len(label) <= 12 else f"{label[:11]}…"
        bounds = draw.textbbox((0, 0), shortened, font=font)
        draw.text(
            (center - (bounds[2] - bounds[0]) / 2, plot[3] + 8),
            shortened,
            fill=(80, 91, 86),
            font=font,
        )
    for value, y in ((maximum, plot[1]), (0.0, axis_y), (minimum, plot[3])):
        label = f"{value:g}"
        bounds = draw.textbbox((0, 0), label, font=font)
        draw.text(
            (plot[0] - (bounds[2] - bounds[0]) - 8, y - 8),
            label,
            fill=(80, 91, 86),
            font=font,
        )
    if bool(getattr(chart, "has_legend", False)):
        legend_x = plot[0]
        legend_y = bottom - 24
        for index, item in enumerate(series):
            name = str(getattr(item, "name", ""))
            if not name:
                return False
            color = palette[index % len(palette)]
            draw.rectangle((legend_x, legend_y, legend_x + 12, legend_y + 12), fill=color)
            draw.text((legend_x + 18, legend_y - 2), name, fill=(65, 75, 71), font=font)
            legend_x += round(28 + draw.textbbox((0, 0), name, font=font)[2])
    # Force font resolution for every label before claiming the chart was handled.
    if labels:
        _bundled_unicode_font(14)
    return True


def _draw_text_frame(
    draw: ImageDraw.ImageDraw,
    shape: Any,
    box: tuple[int, int, int, int],
    pixels_per_emu: float,
) -> bool:
    frame = shape.text_frame
    left = box[0] + round(int(frame.margin_left or 0) * pixels_per_emu)
    right = box[2] - round(int(frame.margin_right or 0) * pixels_per_emu)
    top = box[1] + round(int(frame.margin_top or 0) * pixels_per_emu)
    bottom = box[3] - round(int(frame.margin_bottom or 0) * pixels_per_emu)
    available_width = max(1, right - left)
    prepared: list[
        tuple[
            list[str],
            _ResolvedFont,
            tuple[int, int, int],
            Any,
            int,
            int,
        ]
    ] = []
    total_height = 0
    for paragraph in frame.paragraphs:
        text = paragraph.text
        if not text and len(frame.paragraphs) == 1:
            continue
        run = paragraph.runs[0] if paragraph.runs else None
        run_size = run.font.size if run is not None else None
        paragraph_size = paragraph.font.size
        size_pt = float(
            run_size.pt
            if run_size is not None
            else paragraph_size.pt
            if paragraph_size is not None
            else 18
        )
        bold = bool(
            getattr(getattr(run, "font", None), "bold", None)
            if run is not None and run.font.bold is not None
            else paragraph.font.bold
        )
        font = _font(text, size_pt, bold=bold, pixels_per_emu=pixels_per_emu)
        color = _font_color(run, paragraph)
        lines = _wrap_text(draw, text, font, available_width) or [""]
        line_height = max(1, round(_font_height(font) * 1.12))
        paragraph_height = line_height * len(lines)
        paragraph_after = 0
        if paragraph.space_after is not None:
            paragraph_after = round(int(paragraph.space_after) * pixels_per_emu)
            paragraph_height += paragraph_after
        prepared.append((lines, font, color, paragraph.alignment, line_height, paragraph_after))
        total_height += paragraph_height
    available_height = max(0, bottom - top)
    overflow = total_height > available_height + 2
    anchor = frame.vertical_anchor
    y = top
    if anchor == MSO_ANCHOR.MIDDLE:
        y += max(0, (available_height - total_height) // 2)
    elif anchor == MSO_ANCHOR.BOTTOM:
        y += max(0, available_height - total_height)
    for lines, font, color, alignment, line_height, paragraph_after in prepared:
        for line in lines:
            line_width = _text_width(draw, line, font)
            x = left
            if alignment == PP_ALIGN.CENTER:
                x += max(0, (available_width - line_width) // 2)
            elif alignment == PP_ALIGN.RIGHT:
                x += max(0, available_width - line_width)
            draw.text(
                (x, y),
                line,
                font=font.font,
                fill=color,
                stroke_width=font.stroke_width,
                stroke_fill=color,
            )
            y += line_height
        y += paragraph_after
    return overflow


def _glyph_signature(
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    character: str,
) -> tuple[tuple[int, int], tuple[int, int, int, int] | None, bytes]:
    mask = font.getmask(character, mode="L")
    return mask.size, mask.getbbox(), bytes(mask)


def _missing_characters(
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    text: str,
) -> tuple[str, ...]:
    """Detect characters rendered with a font's generic missing-glyph box."""

    sentinel = _glyph_signature(font, "\U0010ffff")
    missing: list[str] = []
    for character in dict.fromkeys(text):
        if character.isspace() or category(character) in {"Cc", "Cf"}:
            continue
        if _glyph_signature(font, character) == sentinel:
            missing.append(character)
    return tuple(missing)


@lru_cache(maxsize=96)
def _bundled_unicode_font(size_px: int) -> ImageFont.FreeTypeFont:
    """Load PyMuPDF's bundled CJK font so QA does not depend on host fonts."""

    raw = _pymupdf.Font("china-s").buffer
    return ImageFont.truetype(io.BytesIO(raw), size_px)


def _font(
    text: str,
    size_pt: float,
    *,
    bold: bool,
    pixels_per_emu: float,
) -> _ResolvedFont:
    # 914400 EMU/in and 72 pt/in; this preserves the presentation's physical type size.
    size_px = max(8, round(size_pt * pixels_per_emu * 914400 / 72))
    configured = os.environ.get(
        "WORKPILOT_PPTX_FONT_BOLD" if bold else "WORKPILOT_PPTX_FONT", ""
    ).strip()
    candidates = ((Path(configured),) if configured else ()) + (
        _BOLD_FONTS if bold else _REGULAR_FONTS
    )
    for candidate in candidates:
        if candidate.is_file():
            try:
                candidate_font = ImageFont.truetype(str(candidate), size_px)
            except OSError:
                continue
            if not _missing_characters(candidate_font, text):
                name = candidate.name.casefold()
                synthetic_stroke = max(1, size_px // 40) if bold and "bold" not in name else 0
                return _ResolvedFont(candidate_font, synthetic_stroke)
    try:
        bundled = _bundled_unicode_font(size_px)
    except (RuntimeError, OSError, ValueError) as error:  # pragma: no cover - package corruption
        raise PptxRasterError("内置 Unicode 字体无法加载") from error
    missing = _missing_characters(bundled, text)
    if missing:
        preview = "".join(missing[:8])
        suffix = "…" if len(missing) > 8 else ""
        raise PptxRasterError(f"字体缺少字形：{preview}{suffix}")
    return _ResolvedFont(bundled, max(1, size_px // 40) if bold else 0)


def _font_color(run: Any, paragraph: Any) -> tuple[int, int, int]:
    for font in (getattr(run, "font", None), paragraph.font):
        if font is None:
            continue
        try:
            rgb = font.color.rgb
        except (AttributeError, ValueError, TypeError):
            continue
        if rgb is not None:
            return int(rgb[0]), int(rgb[1]), int(rgb[2])
    return 23, 33, 29


def _font_height(font: _ResolvedFont) -> int:
    _left, top, _right, bottom = font.font.getbbox("Ag国")
    return max(1, math.ceil(bottom - top) + font.stroke_width * 2)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: _ResolvedFont) -> int:
    if not text:
        return 0
    box = draw.textbbox(
        (0, 0),
        text,
        font=font.font,
        stroke_width=font.stroke_width,
    )
    return max(0, math.ceil(box[2] - box[0]))


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: _ResolvedFont,
    max_width: int,
) -> list[str]:
    lines: list[str] = []
    for explicit_line in text.splitlines() or [""]:
        if not explicit_line:
            lines.append("")
            continue
        current = ""
        for character in explicit_line:
            candidate = current + character
            if current and _text_width(draw, candidate, font) > max_width:
                lines.append(current.rstrip())
                current = character.lstrip() if character.isspace() else character
            else:
                current = candidate
        lines.append(current.rstrip())
    return lines


__all__ = [
    "PptxRasterError",
    "PptxRasterResult",
    "render_presentation_pages",
]
