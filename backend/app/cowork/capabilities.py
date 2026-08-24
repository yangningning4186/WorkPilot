"""WorkMode 与可组合 Capability 协议。

Capability 只拥有三样东西：稳定 system block、首轮 pre-loop、工具面。它不执行工具，也
不授予权限；所有调用仍经过 ``CoworkToolRegistry.execute`` 的 capability、审批和租约边界。
``exclusive`` 只收窄工具面，不能借此扩大权限。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from app.cowork.work_modes import render_work_mode_block
from app.cowork_contracts import CoworkWorkMode


@dataclass(frozen=True)
class CapabilityActivation:
    goal: str
    work_mode: CoworkWorkMode
    reading_path: str | None = None
    kb_slug: str | None = None
    persona_name: str | None = None


@dataclass(frozen=True)
class CapabilityPreLoopContext:
    state: Mapping[str, Any]
    services: Mapping[str, Any]


CapabilityPredicate = Callable[[CapabilityActivation], bool]
SystemBlockBuilder = Callable[[CapabilityActivation], str]
PreLoopHook = Callable[[CapabilityPreLoopContext], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True)
class WorkCapability:
    name: str
    owned_tools: frozenset[str] = frozenset()
    exclusive: bool = False
    applies: CapabilityPredicate = lambda _: True
    system_block: SystemBlockBuilder = lambda _: ""
    pre_loop: PreLoopHook | None = None


@dataclass(frozen=True)
class ResolvedCapabilities:
    capabilities: tuple[WorkCapability, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.capabilities)

    @property
    def owned_tools(self) -> frozenset[str]:
        return frozenset(
            tool for capability in self.capabilities for tool in capability.owned_tools
        )

    @property
    def exclusive(self) -> bool:
        return any(item.exclusive for item in self.capabilities)

    def render_system_block(self, activation: CapabilityActivation) -> str:
        return "\n\n".join(
            block
            for capability in self.capabilities
            if (block := capability.system_block(activation).strip())
        )

    async def run_pre_loop(self, context: CapabilityPreLoopContext) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for capability in self.capabilities:
            if capability.pre_loop is None:
                continue
            values = await capability.pre_loop(context)
            overlap = output.keys() & values.keys()
            if overlap:
                raise ValueError(f"Capability pre_loop 输出键冲突: {sorted(overlap)}")
            output.update(values)
        return output


class WorkCapabilityRegistry:
    def __init__(self, capabilities: tuple[WorkCapability, ...]) -> None:
        names = [item.name for item in capabilities]
        if len(names) != len(set(names)):
            raise ValueError("Capability name 重复")
        self._capabilities = capabilities

    def resolve(self, activation: CapabilityActivation) -> ResolvedCapabilities:
        active = tuple(item for item in self._capabilities if item.applies(activation))
        exclusive = [item.name for item in active if item.exclusive]
        if len(exclusive) > 1:
            raise ValueError(f"多个 exclusive Capability 同时激活: {exclusive}")
        if exclusive:
            selected = next(item for item in active if item.exclusive)
            active = (selected,)
        return ResolvedCapabilities(active)


def _office_applies(context: CapabilityActivation) -> bool:
    return context.work_mode == "office"


def _reading_applies(context: CapabilityActivation) -> bool:
    return context.work_mode == "reading"


def _knowledge_applies(context: CapabilityActivation) -> bool:
    return bool((context.kb_slug or "").strip())


def _mode_block(context: CapabilityActivation) -> str:
    return render_work_mode_block(context.work_mode, reading_path=context.reading_path)


def build_work_capability_registry(
    *,
    reading_pre_loop: PreLoopHook | None = None,
    knowledge_pre_loop: PreLoopHook | None = None,
) -> WorkCapabilityRegistry:
    """内建能力。以后新增研究/会议复盘只需追加一项，不改 Cowork 主循环。"""

    return WorkCapabilityRegistry(
        (
            WorkCapability(
                name="office",
                owned_tools=frozenset(
                    {
                        "list_files",
                        "read_text_file",
                        "read_pdf",
                        "write_text_file",
                        "create_artifact",
                        "run_shell",
                        "run_sandbox",
                        "list_skills",
                        "load_skill",
                        "load_skill_resource",
                    }
                ),
                applies=_office_applies,
                system_block=_mode_block,
            ),
            WorkCapability(
                name="reading",
                owned_tools=frozenset(
                    {
                        "material_outline",
                        "search_material",
                        "read_material",
                        "reader_goto",
                        "reader_annotate",
                    }
                ),
                applies=_reading_applies,
                system_block=_mode_block,
                pre_loop=reading_pre_loop,
            ),
            WorkCapability(
                name="knowledge",
                owned_tools=frozenset({"search_knowledge"}),
                applies=_knowledge_applies,
                pre_loop=knowledge_pre_loop,
            ),
        )
    )


__all__ = [
    "CapabilityActivation",
    "CapabilityPreLoopContext",
    "ResolvedCapabilities",
    "WorkCapability",
    "WorkCapabilityRegistry",
    "build_work_capability_registry",
]
