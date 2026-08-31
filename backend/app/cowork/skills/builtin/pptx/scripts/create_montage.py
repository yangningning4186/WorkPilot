"""Create a slide-sorter overview from rendered PNG pages.

Adapted from the montage helper in the local ``slides`` skill bundle.  This version is
intentionally limited to already-rasterized pages, so the core preview path needs only
Pillow and does not depend on LibreOffice, ImageMagick, or Inkscape.
"""

# Copyright (c) OpenAI. All rights reserved.

from __future__ import annotations

from collections.abc import Sequence
from math import ceil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


def create_slide_montage(
    pages: Sequence[Path],
    target: Path,
    *,
    columns: int = 4,
    cell_width: int = 350,
    cell_height: int = 197,
    gap: int = 16,
) -> Path:
    """Write a labelled overview image for one complete deck."""

    if not pages:
        raise ValueError("蒙太奇至少需要一页")
    if columns < 1 or cell_width < 80 or cell_height < 45 or gap < 0:
        raise ValueError("蒙太奇尺寸参数无效")
    rows = ceil(len(pages) / columns)
    label_height = 26
    canvas = Image.new(
        "RGB",
        (
            columns * cell_width + (columns + 1) * gap,
            rows * (cell_height + label_height) + (rows + 1) * gap,
        ),
        (242, 242, 242),
    )
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    for index, page in enumerate(pages, start=1):
        column = (index - 1) % columns
        row = (index - 1) // columns
        left = gap + column * (cell_width + gap)
        top = gap + row * (cell_height + label_height + gap)
        with Image.open(page) as source:
            thumbnail = ImageOps.contain(
                source.convert("RGB"),
                (cell_width, cell_height),
                method=Image.Resampling.LANCZOS,
            )
        image_left = left + (cell_width - thumbnail.width) // 2
        image_top = top + (cell_height - thumbnail.height) // 2
        canvas.paste(thumbnail, (image_left, image_top))
        draw.rectangle(
            (
                image_left - 1,
                image_top - 1,
                image_left + thumbnail.width,
                image_top + thumbnail.height,
            ),
            outline=(150, 150, 150),
            width=1,
        )
        label = str(index)
        box = draw.textbbox((0, 0), label, font=font)
        draw.text(
            (left + (cell_width - (box[2] - box[0])) // 2, top + cell_height + 7),
            label,
            fill=(25, 25, 25),
            font=font,
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, format="PNG", optimize=True)
    return target


__all__ = ["create_slide_montage"]
