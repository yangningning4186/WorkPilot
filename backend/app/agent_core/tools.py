"""与具体 Agent、权限系统和工具实现无关的 Tool Registry。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any, Protocol, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ValidationError

from workpilot_ai.types import ToolDefinition


class ToolRegistryError(RuntimeError):
    """工具注册或调用契约不合法。"""


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
        self._tools[spec.name] = spec

    def get(self, name: str) -> SpecT:
        try:
            return self._tools[name]
        except KeyError as error:
            raise self._error(f"未知工具 {name!r}，请从工具目录中重新选择") from error

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

        self._activated_tools.update(name for name in names if name in self._tools)

    def activated_tools_from_snapshot(self, snapshot: dict[str, Any]) -> frozenset[str]:
        """读取 checkpoint 中仍由当前 registry 提供的工具，不修改 registry。"""

        registry_state = snapshot.get("tool_registry")
        if not isinstance(registry_state, dict):
            return frozenset()
        activated = registry_state.get("activated_tools")
        if not isinstance(activated, list):
            return frozenset()
        return frozenset(
            name for name in activated if isinstance(name, str) and name in self._tools
        )

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
        self.activate_tools({name for _, name, _ in selected})
        return [spec.catalog_entry() for _, _, spec in selected]

    def parallel_safe(self, names: list[str]) -> bool:
        if len(names) < 2:
            return False
        try:
            specs = [self.get(name) for name in names]
        except ToolRegistryError:
            return False
        return all(spec.risk == "read" and spec.parallel_safe for spec in specs)

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

        独占不等于需要审批：浏览器动作已经由会话级授权放行，但它们共享一份随页面
        变化而重建的控件编号表，同一批里的第二个动作会拿到失效的 control_index。
        """

        try:
            return self.get(name).exclusive
        except ToolRegistryError:
            return False

    def parse_arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = self.get(name)
        if spec.input_schema is not None:
            try:
                Draft202012Validator.check_schema(spec.input_schema)
                Draft202012Validator(spec.input_schema).validate(arguments)
            except (SchemaError, JsonSchemaValidationError) as error:
                raise self._error(
                    f"工具 {name} 参数不符合 MCP schema：{error.message}"
                ) from error
        try:
            parsed = spec.args_model.model_validate(arguments)
        except ValidationError as error:
            raise self._error(
                f"工具 {name} 参数不符合 schema：{error.errors(include_url=False)}"
            ) from error
        return parsed.model_dump(mode="json")
