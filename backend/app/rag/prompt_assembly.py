"""可审计、可测试的 system prompt 组装器。

system prompt 只由代码中的稳定策略段组成。会话历史、长期记忆和检索证据都是
请求级数据，必须留在 user message，不能混进可缓存的稳定前缀。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SystemPromptSection:
    name: str
    content: str


def assemble_system_prompt(*sections: SystemPromptSection) -> str:
    """按声明顺序拼接稳定策略，并拒绝空段或重名段。"""

    names: set[str] = set()
    parts: list[str] = []
    for section in sections:
        name = section.name.strip()
        content = section.content.strip()
        if not name or not content:
            raise ValueError("system prompt 段的名称和内容不能为空")
        if name in names:
            raise ValueError(f"system prompt 段重名: {name}")
        names.add(name)
        parts.append(content)
    if not parts:
        raise ValueError("system prompt 至少需要一个策略段")
    return "\n\n".join(parts)
