"""Cowork 提示词的稳定分块协议。

块名让模型和维护者都能看见每段规则负责什么；分隔线阻止工具、Persona、模式与资料块
在拼接后变成一段没有边界的长文本。空块不渲染，避免默认模式白占稳定前缀。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptBlock:
    name: str
    content: str


def render_prompt_blocks(blocks: Iterable[PromptBlock]) -> str:
    rendered: list[str] = []
    for block in blocks:
        content = block.content.strip()
        if not content:
            continue
        rendered.append(f"## {block.name}\n\n{content}")
    return "\n\n---\n\n".join(rendered)


__all__ = ["PromptBlock", "render_prompt_blocks"]
