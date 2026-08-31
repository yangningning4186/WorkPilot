"""Local image validation and deterministic SVG raster fallbacks."""

from __future__ import annotations

import base64
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pymupdf
from PIL import Image

_pymupdf: Any = pymupdf

_MAX_IMAGE_BYTES = 16 * 1024 * 1024
_MAX_RASTER_PIXELS = 80_000_000
_RASTER_MEDIA_TYPES = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
}
_FORBIDDEN_SVG_ELEMENTS = frozenset(
    {"audio", "embed", "foreignobject", "iframe", "image", "object", "script", "style", "video"}
)
_CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)


class ArtifactImageError(ValueError):
    """A local visual asset is unsafe or cannot be rendered deterministically."""


def _local_name(value: str) -> str:
    return value.rsplit("}", maxsplit=1)[-1]


def _read_image_bytes(source: Path) -> bytes:
    if source.is_symlink() or not source.is_file():
        raise ArtifactImageError("图片必须是已授权的本地普通文件")
    size = source.stat().st_size
    if size <= 0 or size > _MAX_IMAGE_BYTES:
        raise ArtifactImageError(f"图片大小 {size} bytes 不在允许范围内")
    return source.read_bytes()


def sanitized_svg_bytes(source: Path) -> bytes:
    """Return an SVG after rejecting active or externally loaded content."""

    raw = _read_image_bytes(source)
    lowered = raw.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ArtifactImageError("SVG 不能包含 DTD 或实体声明")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as error:
        raise ArtifactImageError("SVG XML 无法解析") from error
    if _local_name(root.tag) != "svg":
        raise ArtifactImageError("SVG 根元素必须是 <svg>")
    for element in root.iter():
        if _local_name(element.tag).casefold() in _FORBIDDEN_SVG_ELEMENTS:
            raise ArtifactImageError("SVG 包含脚本、嵌入对象或外部媒体元素")
        for raw_name, raw_value in element.attrib.items():
            name = _local_name(raw_name).casefold()
            value = raw_value.strip()
            lowered_value = value.casefold()
            if (
                name.startswith("on")
                or "javascript:" in lowered_value
                or "@import" in lowered_value
            ):
                raise ArtifactImageError("SVG 包含活动内容")
            if name in {"href", "src"} and value and not value.startswith("#"):
                raise ArtifactImageError("SVG 不能引用外部或内嵌资源")
            for match in _CSS_URL.finditer(value):
                if not match.group(2).strip().startswith("#"):
                    raise ArtifactImageError("SVG 样式不能引用外部资源")
    return raw


def image_dimensions(source: Path) -> tuple[int, int]:
    suffix = source.suffix.casefold()
    if suffix == ".svg":
        raw = sanitized_svg_bytes(source)
        with _pymupdf.open(stream=raw, filetype="svg") as document:
            if document.page_count != 1:
                raise ArtifactImageError("SVG 必须只包含一个画布")
            rect = document[0].rect
            width, height = round(rect.width), round(rect.height)
    elif suffix in _RASTER_MEDIA_TYPES:
        _read_image_bytes(source)
        try:
            with Image.open(source) as image:
                image.verify()
            with Image.open(source) as image:
                width, height = image.size
        except (OSError, ValueError) as error:
            raise ArtifactImageError("图片文件已损坏或格式不受支持") from error
    else:
        raise ArtifactImageError("图片仅支持 SVG、PNG、JPEG 或 GIF")
    if width <= 0 or height <= 0:
        raise ArtifactImageError("图片尺寸无效")
    if suffix != ".svg" and (width * height > _MAX_RASTER_PIXELS or max(width, height) > 16_384):
        raise ArtifactImageError("位图像素尺寸超过安全上限")
    return width, height


def _rasterize_svg(source: Path, target: Path, *, max_dimension: int) -> None:
    raw = sanitized_svg_bytes(source)
    try:
        with _pymupdf.open(stream=raw, filetype="svg") as document:
            if document.page_count != 1:
                raise ArtifactImageError("SVG 必须只包含一个画布")
            page = document[0]
            largest = max(page.rect.width, page.rect.height)
            if largest <= 0:
                raise ArtifactImageError("SVG 画布尺寸无效")
            scale = max_dimension / largest
            pixmap = page.get_pixmap(matrix=_pymupdf.Matrix(scale, scale), alpha=True)
            pixmap.save(str(target))
    except ArtifactImageError:
        raise
    except (RuntimeError, ValueError) as error:
        raise ArtifactImageError("SVG 无法转换为兼容 PNG") from error


@contextmanager
def compatible_raster_path(source: Path, *, max_dimension: int = 1600) -> Iterator[Path]:
    """Yield a validated raster path, converting a safe local SVG when needed."""

    if max_dimension < 256 or max_dimension > 4096:
        raise ValueError("max_dimension 必须在 256–4096 之间")
    if source.suffix.casefold() != ".svg":
        image_dimensions(source)
        yield source
        return
    with tempfile.TemporaryDirectory(prefix="workpilot-svg-") as raw_directory:
        target = Path(raw_directory) / "asset.png"
        _rasterize_svg(source, target, max_dimension=max_dimension)
        image_dimensions(target)
        yield target


def image_data_uri(source: Path) -> str:
    """Build a self-contained data URI after applying the same local-asset checks."""

    suffix = source.suffix.casefold()
    if suffix == ".svg":
        media_type = "image/svg+xml"
        raw = sanitized_svg_bytes(source)
    elif suffix in _RASTER_MEDIA_TYPES:
        image_dimensions(source)
        media_type = _RASTER_MEDIA_TYPES[suffix]
        raw = source.read_bytes()
    else:
        raise ArtifactImageError("图片仅支持 SVG、PNG、JPEG 或 GIF")
    return f"data:{media_type};base64,{base64.b64encode(raw).decode('ascii')}"


__all__ = [
    "ArtifactImageError",
    "compatible_raster_path",
    "image_data_uri",
    "image_dimensions",
    "sanitized_svg_bytes",
]
