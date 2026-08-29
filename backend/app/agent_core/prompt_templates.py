"""Strict, provider-neutral parameterized prompt templates."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

_PARAMETER = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_.-]{0,63})\s*}}")
_TEMPLATE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}")


class PromptTemplateError(ValueError):
    pass


class DuplicatePromptTemplateError(PromptTemplateError):
    pass


@dataclass(frozen=True)
class PromptTemplate:
    template_id: str
    source: str
    max_rendered_chars: int = 100_000

    def __post_init__(self) -> None:
        if _TEMPLATE_ID.fullmatch(self.template_id) is None:
            raise PromptTemplateError(f"非法 prompt template id: {self.template_id!r}")
        if not self.source:
            raise PromptTemplateError(f"prompt template {self.template_id} 不能为空")
        if self.max_rendered_chars < 1:
            raise PromptTemplateError("max_rendered_chars 必须为正整数")
        remainder = _PARAMETER.sub("", self.source)
        if "{{" in remainder or "}}" in remainder:
            raise PromptTemplateError(f"prompt template {self.template_id} 含有不完整占位符")

    @property
    def parameters(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(_PARAMETER.findall(self.source)))

    def render(self, arguments: Mapping[str, object]) -> str:
        required = set(self.parameters)
        supplied = set(arguments)
        missing = sorted(required - supplied)
        extra = sorted(supplied - required)
        if missing:
            raise PromptTemplateError(
                f"prompt template {self.template_id} 缺少参数: {', '.join(missing)}"
            )
        if extra:
            raise PromptTemplateError(
                f"prompt template {self.template_id} 收到未知参数: {', '.join(extra)}"
            )
        values: dict[str, str] = {}
        for name in self.parameters:
            raw = arguments[name]
            if not isinstance(raw, str | int | float | bool):
                raise PromptTemplateError(f"prompt template 参数 {name} 必须是标量")
            values[name] = str(raw)
        # Exactly one substitution pass: template-looking text inside an argument remains data.
        rendered = _PARAMETER.sub(lambda match: values[match.group(1)], self.source)
        if len(rendered) > self.max_rendered_chars:
            raise PromptTemplateError(
                f"prompt template {self.template_id} 渲染后超过 {self.max_rendered_chars} 字符"
            )
        return rendered


class PromptTemplateRegistry:
    """ID-addressed registry; duplicate registration is always a configuration error."""

    def __init__(self) -> None:
        self._templates: dict[str, PromptTemplate] = {}

    def register(self, template: PromptTemplate) -> None:
        if template.template_id in self._templates:
            raise DuplicatePromptTemplateError(template.template_id)
        self._templates[template.template_id] = template

    def get(self, template_id: str) -> PromptTemplate:
        try:
            return self._templates[template_id]
        except KeyError as error:
            raise PromptTemplateError(f"prompt template 不存在: {template_id}") from error

    def render(self, template_id: str, arguments: Mapping[str, object]) -> str:
        return self.get(template_id).render(arguments)

    def catalog(self) -> tuple[PromptTemplate, ...]:
        return tuple(self._templates[name] for name in sorted(self._templates))


__all__ = [
    "DuplicatePromptTemplateError",
    "PromptTemplate",
    "PromptTemplateError",
    "PromptTemplateRegistry",
]
