"""兼容入口：PPTX Renderer 由内置 pptx Skill 自己携带。"""

from app.cowork.skills.builtin.pptx.scripts.render_pptx import render_presentation

__all__ = ["render_presentation"]
