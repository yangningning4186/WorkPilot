"""轻量 Persona：提示风格、工具面、默认审批档与推荐连接器的组合描述。

Persona 不注册工具、不授予 capability，也不创建审批规则。它只能收窄模型看到/可调用的
工具；默认审批档只有用户在界面显式选择 Persona 时才写入会话。用户层位于
``<cowork_data_path>/personas/*.toml``，项目层位于已授权根目录的
``.workpilot/personas/*.toml``，优先级 project > user > builtin。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.core.config import Settings
from app.cowork.connector_descriptors import connector_kinds
from app.cowork_contracts import ApprovalMode, CoworkWorkMode

PersonaOrigin = Literal["builtin", "user", "project"]
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TOOL_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}\*?$")
PROJECT_PERSONAS_RELATIVE = Path(".workpilot") / "personas"


@dataclass(frozen=True)
class PersonaDefinition:
    name: str
    label: str
    description: str
    system_block: str
    tool_patterns: tuple[str, ...]
    default_approval_mode: ApprovalMode
    recommended_connectors: tuple[str, ...]
    recommended_work_mode: CoworkWorkMode
    origin: PersonaOrigin = "builtin"
    source_path: Path | None = None

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "tool_patterns": list(self.tool_patterns),
            "default_approval_mode": self.default_approval_mode,
            "recommended_connectors": list(self.recommended_connectors),
            "recommended_work_mode": self.recommended_work_mode,
            "origin": self.origin,
        }


@dataclass(frozen=True)
class PersonaCatalog:
    personas: tuple[PersonaDefinition, ...]
    errors: tuple[str, ...] = ()

    def get(self, name: str) -> PersonaDefinition:
        for persona in self.personas:
            if persona.name == name:
                return persona
        raise ValueError(f"未知 Persona: {name}")


_BUILTINS = (
    PersonaDefinition(
        name="general",
        label="通用执行",
        description="完整 Cowork 工具面，逐次审批，适合混合型办公任务。",
        system_block="",
        tool_patterns=(),
        default_approval_mode="interactive",
        recommended_connectors=(),
        recommended_work_mode="office",
    ),
    PersonaDefinition(
        name="office-operator",
        label="办公执行员",
        description="聚焦本地文档、Office 交付物与中国办公连接器。",
        system_block=(
            '<persona name="office-operator">\n'
            "以可直接打开、复核和转交的办公交付物为完成标准。保留原文件未要求修改的结构与格式；\n"
            "外部写入前核对账户、目标对象、动作范围与预期结果，完成后报告实际返回状态。\n"
            "</persona>"
        ),
        tool_patterns=(
            "list_*",
            "read_*",
            "search_*",
            "write_file",
            "replace_in_file",
            "run_shell",
            "shell_task_*",
            "load_skill",
            "feishu_*",
        ),
        default_approval_mode="interactive",
        recommended_connectors=("feishu", "tencent_docs"),
        recommended_work_mode="office",
    ),
    PersonaDefinition(
        name="meeting-secretary",
        label="会议秘书",
        description="围绕会前材料、日程、任务、审批和会后纪要组织工作。",
        system_block=(
            '<persona name="meeting-secretary">\n'
            "把信息分为议题、已确认决定、行动项、责任人、截止时间和待确认项；缺失字段写明未确认。\n"
            "讨论、建议和倾向不得升级成决定；相对时间要落到运行环境中的明确日期后再写入外部系统。\n"
            "</persona>"
        ),
        tool_patterns=(
            "list_*",
            "read_*",
            "search_*",
            "material_*",
            "reader_*",
            "create_*",
            "feishu_calendar_*",
            "feishu_document_*",
            "feishu_drive_*",
            "feishu_task_*",
            "feishu_approval_*",
        ),
        default_approval_mode="interactive",
        recommended_connectors=("feishu",),
        recommended_work_mode="office",
    ),
    PersonaDefinition(
        name="researcher",
        label="深度研究员",
        description="聚焦资料读取、网页与知识库检索，默认不暴露外部写入工具。",
        system_block=(
            '<persona name="researcher">\n'
            "先收集证据再综合。关键结论紧跟可追溯来源，并区分来源陈述、你的推断与未解决缺口；\n"
            "不要把检索摘要当全文，也不要为了显得完整而补写资料中没有的结论。\n"
            "</persona>"
        ),
        tool_patterns=(
            "list_*",
            "read_*",
            "search_*",
            "fetch_url",
            "web_search",
            "browser_snapshot",
            "material_*",
            "reader_goto",
            "write_file",
        ),
        default_approval_mode="interactive",
        recommended_connectors=("feishu", "tencent_docs", "github"),
        recommended_work_mode="reading",
    ),
)


def _strings(value: object, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Persona {field} 必须是字符串数组")
    return tuple(item.strip() for item in value if item.strip())


def _load_file(path: Path, *, origin: PersonaOrigin) -> PersonaDefinition:
    if path.is_symlink() or path.stat().st_size > 64_000:
        raise ValueError("Persona 文件必须是 64KB 内的普通 TOML")
    loaded = tomllib.loads(path.read_text(encoding="utf-8"))
    name = str(loaded.get("name") or path.stem).strip()
    label = str(loaded.get("label") or "").strip()
    description = str(loaded.get("description") or "").strip()
    block = str(loaded.get("system_block") or "").strip()
    patterns = _strings(loaded.get("tool_patterns"), field="tool_patterns")
    connectors = _strings(loaded.get("recommended_connectors"), field="recommended_connectors")
    approval = str(loaded.get("default_approval_mode") or "interactive")
    work_mode = str(loaded.get("recommended_work_mode") or "office")
    if not _NAME.fullmatch(name) or path.stem != name:
        raise ValueError("Persona name 必须合法并与文件名一致")
    if not 1 <= len(label) <= 64 or not 1 <= len(description) <= 500 or len(block) > 8_000:
        raise ValueError("Persona label/description/system_block 长度非法")
    if any(_TOOL_PATTERN.fullmatch(item) is None for item in patterns):
        raise ValueError("Persona tool_patterns 只允许工具名或末尾通配符 *")
    if any(item not in connector_kinds() for item in connectors):
        raise ValueError("Persona recommended_connectors 含未知连接器")
    if approval not in {"interactive", "auto"} or work_mode not in {"office", "reading"}:
        raise ValueError("Persona 默认审批档或工作模式非法")
    return PersonaDefinition(
        name=name,
        label=label,
        description=description,
        system_block=block,
        tool_patterns=patterns,
        default_approval_mode=approval,  # type: ignore[arg-type]
        recommended_connectors=connectors,
        recommended_work_mode=work_mode,  # type: ignore[arg-type]
        origin=origin,
        source_path=path,
    )


def _scan(root: Path, *, origin: PersonaOrigin) -> tuple[list[PersonaDefinition], list[str]]:
    if not root.exists():
        return [], []
    if not root.is_dir() or root.is_symlink():
        return [], [f"{root}: Persona 根目录不是普通目录"]
    personas: list[PersonaDefinition] = []
    errors: list[str] = []
    for path in sorted(root.glob("*.toml"))[:100]:
        try:
            personas.append(_load_file(path, origin=origin))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError) as error:
            errors.append(f"{path}: {error}")
    return personas, errors


def load_persona_catalog(
    settings: Settings,
    *,
    project_roots: tuple[Path, ...] = (),
) -> PersonaCatalog:
    effective = {item.name: item for item in _BUILTINS}
    errors: list[str] = []
    user, user_errors = _scan(settings.cowork_data_path.expanduser() / "personas", origin="user")
    errors.extend(user_errors)
    for persona in user:
        effective[persona.name] = persona
    project_names: set[str] = set()
    for root in project_roots:
        project, project_errors = _scan(root / PROJECT_PERSONAS_RELATIVE, origin="project")
        errors.extend(project_errors)
        for persona in project:
            if persona.name in project_names:
                continue
            effective[persona.name] = persona
            project_names.add(persona.name)
    return PersonaCatalog(
        tuple(sorted(effective.values(), key=lambda item: item.name)), tuple(errors)
    )


def tool_name_matches(pattern: str, tool_name: str) -> bool:
    return tool_name.startswith(pattern[:-1]) if pattern.endswith("*") else tool_name == pattern


def approval_mode_for_persona_change(
    *,
    current_name: str,
    requested_mode: ApprovalMode,
    selected: PersonaDefinition,
) -> ApprovalMode:
    """只在真的切 Persona 时应用默认档；换模型等普通 runtime 更新必须原样保留。"""

    return selected.default_approval_mode if selected.name != current_name else requested_mode


__all__ = [
    "PROJECT_PERSONAS_RELATIVE",
    "PersonaCatalog",
    "PersonaDefinition",
    "approval_mode_for_persona_change",
    "load_persona_catalog",
    "tool_name_matches",
]
