"""兼容入口：PPTX 逐页渲染脚本由内置 pptx Skill 自己携带。"""

from app.cowork.skills.builtin.pptx.scripts.pptx2image import (
    PptxRasterError,
    PptxRasterResult,
    render_presentation_pages,
)

__all__ = [
    "PptxRasterError",
    "PptxRasterResult",
    "render_presentation_pages",
]
