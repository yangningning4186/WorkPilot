"""与具体 Agent、权限系统和工具实现无关的 Tool Registry。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from typing import Any, Protocol, cast

from pydantic import BaseModel, ValidationError

from app.agent_core.json_schema import (
    BoundedJsonSchemaError,
    BoundedJsonSchemaValidator,
    BoundedJsonValueError,
    compile_bounded_json_schema,
)
from workpilot_ai.types import ToolDefinition


class ToolRegistryError(RuntimeError):
    """工具注册或调用契约不合法。"""


class MissingIdentitiesError(ToolRegistryError):
    """A resumed run references tools/models that are no longer installed."""

    def __init__(
        self,
        *,
        tools: Iterable[str] = (),
        models: Iterable[str] = (),
    ) -> None:
        self.tools = tuple(sorted(set(tools)))
        self.models = tuple(sorted(set(models)))
        parts = []
        if self.tools:
            parts.append(f"tools={list(self.tools)}")
        if self.models:
            parts.append(f"models={list(self.models)}")
        super().__init__("恢复所需身份已缺失：" + ", ".join(parts or ["unknown"]))


class RegistryToolSpec(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def args_model(self) -> type[BaseModel]: ...

    @property
    def risk(self) -> str: ...

    @property
    def effect(self) -> str: ...

    @property
    def parallel_safe(self) -> bool: ...

    @property
    def handler(self) -> object | None: ...

    @property
    def execution(self) -> str: ...

    @property
    def input_schema(self) -> dict[str, Any] | None: ...

    @property
    def approval_required(self) -> bool: ...

    @property
    def exclusive(self) -> bool: ...

    @property
    def search_aliases(self) -> tuple[str, ...]: ...

    @property
    def prepare_arguments(self) -> Callable[[dict[str, Any]], dict[str, Any]] | None: ...

    @property
    def prompt_snippet(self) -> str: ...

    @property
    def prompt_guidelines(self) -> tuple[str, ...]: ...

    @property
    def execution_mode(self) -> str: ...

    def resolved_input_schema(self) -> dict[str, Any]: ...

    def catalog_entry(self) -> dict[str, Any]: ...


class ToolRegistry[SpecT: RegistryToolSpec]:
    """提供所有 Agent 共用的注册、发现、校验和执行元数据能力。

    权限、路径授权、HITL 与副作用幂等由产品层 registry/adapter 负责。
    """

    error_type: type[ToolRegistryError] = ToolRegistryError

    def __init__(self) -> None:
        self._tools: dict[str, SpecT] = {}
        self._system_instructions: list[str] = []
        self._runtime_snapshot: dict[str, Any] = {}
        self._activated_tools: set[str] = set()
        self._input_schema_validators: dict[str, BoundedJsonSchemaValidator] = {}

    def _error(self, message: str) -> ToolRegistryError:
        return self.error_type(message)

    def register(self, spec: SpecT) -> None:
        if not spec.name or spec.name in self._tools:
            raise ValueError(f"工具名称为空或重复: {spec.name!r}")
        if spec.risk == "write" and spec.effect == "none":
            raise ValueError("写工具必须声明副作用类型")
        if spec.parallel_safe and spec.risk != "read":
            raise ValueError("只有只读工具可以声明 parallel_safe")
        if spec.approval_required and spec.effect == "none":
            raise ValueError("需要审批的工具必须声明副作用")
        if spec.execution == "local" and spec.handler is None:
            raise ValueError("本地工具必须提供 handler")
        if spec.execution == "interaction" and spec.handler is not None:
            raise ValueError("交互工具由 runtime 挂起处理，不能提供 handler")
        validator: BoundedJsonSchemaValidator | None = None
        if spec.input_schema is not None:
            try:
                validator = compile_bounded_json_schema(spec.input_schema)
            except BoundedJsonSchemaError:
                raise ValueError("外部工具 input schema 不安全或不受支持") from None
        self._tools[spec.name] = spec
        if validator is not None:
            self._input_schema_validators[spec.name] = validator

    def get(self, name: str) -> SpecT:
        try:
            return self._tools[name]
        except KeyError as error:
            raise self._error(f"未知工具 {name!r}，请从工具目录中重新选择") from error

    def names(self) -> frozenset[str]:
        """已注册的全部工具名。评测套件用它校验 gold 里的工具名没有漂移。"""

        return frozenset(self._tools)

    def catalog(self) -> list[dict[str, Any]]:
        return [self._tools[name].catalog_entry() for name in sorted(self._tools)]

    def add_system_instructions(self, instructions: str) -> None:
        normalized = instructions.strip()
        if normalized:
            self._system_instructions.append(normalized)

    def system_instructions(self) -> str:
        return "\n\n".join(self._system_instructions)

    def update_runtime_snapshot(self, key: str, value: Any) -> None:
        self._runtime_snapshot[key] = json.loads(
            json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        )

    def activate_tools(self, names: Iterable[str]) -> None:
        """记录本 run 已向模型暴露的工具；集合在 registry 生命周期内只增不减。"""

        requested = tuple(names)
        missing = tuple(name for name in requested if name not in self._tools)
        if missing:
            raise MissingIdentitiesError(tools=missing)
        self._activated_tools.update(requested)

    def activated_tools_from_snapshot(self, snapshot: dict[str, Any]) -> frozenset[str]:
        """读取 checkpoint 中仍由当前 registry 提供的工具，不修改 registry。"""

        registry_state = snapshot.get("tool_registry")
        if registry_state is None:
            return frozenset()
        if not isinstance(registry_state, dict):
            raise self._error("checkpoint tool_registry 不是对象")
        activated = registry_state.get("activated_tools")
        if activated is None:
            return frozenset()
        if not isinstance(activated, list) or any(not isinstance(name, str) for name in activated):
            raise self._error("checkpoint activated_tools 不是字符串数组")
        missing = tuple(name for name in activated if name not in self._tools)
        if missing:
            raise MissingIdentitiesError(tools=missing)
        return frozenset(activated)

    def restore_runtime_snapshot(self, snapshot: dict[str, Any]) -> None:
        """从 checkpoint 恢复可继续使用、且当前 registry 仍实际注册的工具。"""

        self._activated_tools = set(self.activated_tools_from_snapshot(snapshot))

    def runtime_snapshot(self) -> dict[str, Any]:
        snapshot = {
            **self._runtime_snapshot,
            "tool_registry": {"activated_tools": sorted(self._activated_tools)},
        }
        return cast(
            "dict[str, Any]",
            json.loads(
                json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            ),
        )

    def tool_definitions(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name=self._tools[name].name,
                description=self._tools[name].description,
                parameters=self._tools[name].resolved_input_schema(),
                prompt_snippet=self._tools[name].prompt_snippet,
                prompt_guidelines=self._tools[name].prompt_guidelines,
            )
            for name in sorted(self._tools)
        ]

    def search_tools(self, query: str, *, max_results: int) -> list[dict[str, Any]]:
        terms: list[str] = []
        for item in re.findall(r"[a-z0-9_.-]+|[\u3400-\u9fff]{2,}", query.casefold()):
            if len(item) <= 1:
                continue
            terms.append(item)
            if "\u3400" <= item[0] <= "\u9fff" and len(item) > 3:
                terms.extend(item[index : index + 2] for index in range(len(item) - 1))
        terms = list(dict.fromkeys(terms))
        scored: list[tuple[int, str, SpecT]] = []
        for name, spec in self._tools.items():
            normalized_name = name.casefold()
            normalized_aliases = " ".join(spec.search_aliases).casefold()
            haystack = f"{normalized_name} {spec.description.casefold()} {normalized_aliases}"
            score = sum(
                4 if term in normalized_name else 2 if term in normalized_aliases else 1
                for term in terms
                if term in haystack
            )
            if score:
                scored.append((score, name, spec))
        scored.sort(key=lambda item: (-item[0], item[1]))
        selected = scored[:max_results]
        return [spec.catalog_entry() for _, _, spec in selected]

    def parallel_safe(self, names: list[str]) -> bool:
        if len(names) < 2:
            return False
        try:
            specs = [self.get(name) for name in names]
        except ToolRegistryError:
            return False
        return all(
            spec.risk == "read" and spec.parallel_safe and spec.execution_mode != "sequential"
            for spec in specs
        )

    def is_interaction(self, name: str) -> bool:
        try:
            return self.get(name).execution == "interaction"
        except ToolRegistryError:
            return False

    def requires_approval(self, name: str) -> bool:
        try:
            return self.get(name).approval_required
        except ToolRegistryError:
            return False

    def is_exclusive(self, name: str) -> bool:
        """该工具是否必须独占一批调用。

        interaction 与静态逐次审批天然独占；除此之外由 spec.exclusive 表达。例如浏览器
        动作已经由会话级授权放行，但共享一份随页面变化而重建的控件编号表，同一批里的
        第二个动作会拿到失效的 control_index。
        """

        try:
            spec = self.get(name)
            return spec.exclusive or spec.execution == "interaction" or spec.approval_required
        except ToolRegistryError:
            return False

    def parse_arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = self.get(name)
        prepared = dict(arguments)
        if spec.prepare_arguments is not None:
            try:
                prepared = spec.prepare_arguments(prepared)
            except (TypeError, ValueError) as error:
                raise self._error(f"工具 {name} 参数兼容转换失败：{error}") from error
            if not isinstance(prepared, dict):
                raise self._error(f"工具 {name} 参数兼容转换必须返回 JSON object")
        if spec.input_schema is not None:
            try:
                self._input_schema_validators[name].validate(prepared)
            except (KeyError, BoundedJsonValueError):
                raise self._error(f"工具 {name} 参数不符合已固定的外部 schema") from None
        try:
            parsed = spec.args_model.model_validate(prepared)
        except ValidationError as error:
            if spec.input_schema is not None:
                raise self._error(f"工具 {name} 参数不符合已固定的外部 schema") from None
            raise self._error(
                f"工具 {name} 参数不符合 schema：{error.errors(include_url=False)}"
            ) from error
        return parsed.model_dump(mode="json")


def render_tool_prompt_instructions(tools: Iterable[ToolDefinition]) -> str:
    """只渲染当前实际暴露工具自带的 prompt 文案。"""

    sections: list[str] = []
    for tool in tools:
        snippet = tool.prompt_snippet.strip()
        guidelines = [item.strip() for item in tool.prompt_guidelines if item.strip()]
        if not snippet and not guidelines:
            continue
        lines = [f"[{tool.name}]", snippet] if snippet else [f"[{tool.name}]"]
        lines.extend(f"- {item}" for item in guidelines)
        sections.append("\n".join(lines))
    if not sections:
        return ""
    return "<tool_prompt_guidance>\n" + "\n\n".join(sections) + "\n</tool_prompt_guidance>"
