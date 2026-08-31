"""轻量 Persona：提示风格、工具面、默认审批档与推荐连接器的组合描述。

Persona 不注册工具、不授予 capability，也不创建审批规则。它只能收窄模型看到/可调用的
工具；默认审批档只有用户在界面显式选择 Persona 时才写入会话。用户层位于
``<cowork_data_path>/personas/*.toml``，项目层位于已授权根目录的
``.workpilot/personas/*.toml``，优先级 project > user > builtin。
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

from app.core.config import Settings
from app.cowork.connector_descriptors import connector_kinds, get_connector_descriptor
from app.cowork_contracts import ApprovalMode, CoworkWorkMode

PersonaOrigin = Literal["builtin", "user", "project"]
ExpertType = Literal["agent", "team"]
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TOOL_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}\*?$")
PROJECT_PERSONAS_RELATIVE = Path(".workpilot") / "personas"
PERSONA_SNAPSHOT_SCHEMA: Literal["workpilot.persona-snapshot.v1"] = "workpilot.persona-snapshot.v1"
PERSONA_RESELECTION_REQUIRED = "Persona 定义已变化、缺失或来源不再授权；请重新选择 Persona 后重试"


class PersonaConnectorCapability(TypedDict):
    kind: str
    capabilities: list[str]


class PersonaCapabilitySummary(TypedDict):
    default_approval_mode: ApprovalMode
    recommended_work_mode: CoworkWorkMode
    recommended_connectors: list[PersonaConnectorCapability]


class ExpertTeamMemberSnapshot(TypedDict):
    profile: str
    label: str
    role: str
    reason: str
    tool_patterns: list[str]
    sha256: str


class PersonaSnapshot(TypedDict):
    schema_version: Literal["workpilot.persona-snapshot.v1"]
    name: str
    origin: PersonaOrigin
    source_identity: str
    sha256: str
    tool_patterns: list[str]
    capability_summary: PersonaCapabilitySummary
    # 只在 expert_type=team 时出现，保证既有普通 Persona 的 v1 快照和摘要不漂移。
    expert_type: NotRequired[Literal["team"]]
    team_members: NotRequired[list[ExpertTeamMemberSnapshot]]


@dataclass(frozen=True)
class ExpertTeamMemberDefinition:
    """专家团中的受信 Worker profile；职责、提示词与工具面由包定义而非模型自由编造。"""

    profile: str
    label: str
    role: str
    reason: str
    system_block: str
    tool_patterns: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "label": self.label,
            "role": self.role,
            "reason": self.reason,
            "tool_patterns": list(self.tool_patterns),
        }


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
    expert_type: ExpertType = "agent"
    team_members: tuple[ExpertTeamMemberDefinition, ...] = ()
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
            "expert_type": self.expert_type,
            "team_members": [member.public() for member in self.team_members],
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
        name="expert-council",
        label="深度研究与风险评审团",
        description="并行核验证据和分析方案，再由独立审阅专家检查反例与风险，适合重要决策和复杂方案评审。",
        system_block=(
            '<persona name="expert-council" expert_type="team">\n'
            "你是深度研究与风险评审团 Lead，负责定义问题、拆分可证伪的验收标准、传递上下文和最终裁决，"
            "不得假装已经收到任何 Worker 的结论。简单且不需要独立复核的任务可直接完成；"
            "需要会诊时，先 load_tools(propose_team)，再单独调用 propose_team：expert 必须为 "
            '"expert-council"，expert_sha256 必须原样复制下方 expert_team_manifest 的 '
            "manifest_sha256；成员 profile 从 evidence-researcher、domain-analyst、"
            "critical-reviewer 中选择，建议三者全部启用。name 是本次团队内的英文呼号，"
            "不要传 role，职责以 profile 的固化定义为准。\n"
            "阶段一并行：证据研究专家建立来源、事实与缺口；领域分析专家给出机制、边界条件和"
            "可选方案。Lead 分别创建 Board task 并验收。阶段二串行：把阶段一的原始报告、"
            "冲突点和待决假设完整写入批判审阅任务，由独立审阅专家找反例、风险与证据不足。"
            "最后由 Lead 明确列出共识、分歧、证据、未知项和建议；Worker 只能提交 review，"
            "Lead 必须逐项验收，不能把报告自动当成正确答案。\n"
            "</persona>"
        ),
        tool_patterns=(),
        default_approval_mode="interactive",
        recommended_connectors=(),
        recommended_work_mode="office",
        expert_type="team",
        team_members=(
            ExpertTeamMemberDefinition(
                profile="evidence-researcher",
                label="证据研究专家",
                role="收集并核验与问题直接相关的事实、来源、时间边界和证据缺口",
                reason="先建立可追溯事实层，避免后续分析建立在未核验陈述上",
                system_block=(
                    "你是证据研究专家。只处理 Lead 通过 Board 下发的问题与资源范围。"
                    "先列需要验证的 claim，再逐条寻找直接证据；报告区分来源明确陈述、你的推断"
                    "和仍未解决的缺口。记录文件路径、定位线索与适用时间，冲突来源并列呈现。"
                    "不得把合理猜测写成事实，也不得替 Lead 给出最终业务决策。"
                ),
                tool_patterns=(
                    "list_files",
                    "read_file",
                    "read_text_file",
                    "search_files",
                    "read_pdf",
                ),
            ),
            ExpertTeamMemberDefinition(
                profile="domain-analyst",
                label="领域分析专家",
                role="基于已给事实分析机制、约束、备选方案与适用边界",
                reason="把事实转换为可比较方案，并显式暴露假设与取舍",
                system_block=(
                    "你是领域分析专家。围绕 Board 验收标准建立问题模型：写清目标、约束、关键"
                    "变量、依赖关系和失败条件，再比较方案。每个判断都标明依据是任务上下文、"
                    "工具证据还是待验证假设；信息不足时给出条件化结论。不要伪造行业数据，"
                    "不要越过 Lead 代替用户拍板。"
                ),
                tool_patterns=(
                    "list_files",
                    "read_file",
                    "read_text_file",
                    "search_files",
                    "read_pdf",
                ),
            ),
            ExpertTeamMemberDefinition(
                profile="critical-reviewer",
                label="批判审阅专家",
                role="独立审查前序结论，寻找反例、遗漏、风险和不成立的验收证据",
                reason="让提出方案的人不同时担任自己的最终验证者",
                system_block=(
                    "你是独立批判审阅专家。你的输入应包含其他专家的原始报告；若缺失就把它列为"
                    "阻塞项，不猜测其内容。逐条挑战关键 claim，寻找反例、口径冲突、证据链断点、"
                    "权限与执行风险，并说明什么证据可以消除疑问。不要为了显得有价值而制造异议，"
                    "也不要自行宣告 Board task 已完成；只向 Lead 提交可复核的 review 报告。"
                ),
                tool_patterns=(
                    "list_files",
                    "read_file",
                    "read_text_file",
                    "search_files",
                    "read_pdf",
                ),
            ),
        ),
    ),
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


def _team_members(value: object) -> tuple[ExpertTeamMemberDefinition, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("Persona team_members 必须是 TOML table 数组")
    allowed = {"profile", "label", "role", "reason", "system_block", "tool_patterns"}
    members: list[ExpertTeamMemberDefinition] = []
    for raw in value:
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"Persona team_members 含未知字段 {unknown}")
        profile = str(raw.get("profile") or "").strip().lower()
        label = str(raw.get("label") or "").strip()
        role = " ".join(str(raw.get("role") or "").split())
        reason = " ".join(str(raw.get("reason") or "").split())
        system_block = str(raw.get("system_block") or "").strip()
        patterns = _strings(raw.get("tool_patterns"), field="team_members.tool_patterns")
        if not _NAME.fullmatch(profile):
            raise ValueError("Persona team member profile 必须是合法小写标识")
        if not 1 <= len(label) <= 64 or not 1 <= len(role) <= 160:
            raise ValueError("Persona team member label/role 长度非法")
        if not 1 <= len(reason) <= 500 or not 1 <= len(system_block) <= 8_000:
            raise ValueError("Persona team member reason/system_block 长度非法")
        if any(_TOOL_PATTERN.fullmatch(item) is None for item in patterns):
            raise ValueError("Persona team member tool_patterns 只允许工具名或末尾通配符 *")
        members.append(
            ExpertTeamMemberDefinition(
                profile=profile,
                label=label,
                role=role,
                reason=reason,
                system_block=system_block,
                tool_patterns=patterns,
            )
        )
    profiles = [member.profile for member in members]
    if len(profiles) != len(set(profiles)):
        raise ValueError("Persona team member profile 不能重复")
    return tuple(members)


def _load_file(path: Path, *, origin: PersonaOrigin) -> PersonaDefinition:
    if path.is_symlink() or path.stat().st_size > 64_000:
        raise ValueError("Persona 文件必须是 64KB 内的普通 TOML")
    loaded = tomllib.loads(path.read_text(encoding="utf-8"))
    name = str(loaded.get("name") or path.stem).strip()
    label = str(loaded.get("label") or "").strip()
    description = str(loaded.get("description") or "").strip()
    block = str(loaded.get("system_block") or "").strip()
    expert_type = str(loaded.get("expert_type") or "agent").strip()
    team_members = _team_members(loaded.get("team_members"))
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
    if expert_type not in {"agent", "team"}:
        raise ValueError("Persona expert_type 只能是 agent 或 team")
    if expert_type == "team" and not 2 <= len(team_members) <= 4:
        raise ValueError("专家团 Persona 必须定义 2-4 个 team_members")
    if expert_type == "agent" and team_members:
        raise ValueError("普通 Persona 不能定义 team_members")
    if sum(len(member.system_block) for member in team_members) > 24_000:
        raise ValueError("Persona team member system_block 总长度超过 24KB")
    return PersonaDefinition(
        name=name,
        label=label,
        description=description,
        system_block=block,
        tool_patterns=patterns,
        default_approval_mode=approval,  # type: ignore[arg-type]
        recommended_connectors=connectors,
        recommended_work_mode=work_mode,  # type: ignore[arg-type]
        expert_type=expert_type,  # type: ignore[arg-type]
        team_members=team_members,
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


def snapshot_persona(
    persona: PersonaDefinition,
    settings: Settings,
    *,
    project_roots: tuple[Path, ...] = (),
) -> PersonaSnapshot:
    """Build the canonical, checkpoint-safe identity of a selected Persona.

    The digest covers every field that can change model behavior or the visible capability
    surface, while ``source_identity`` binds project Personas to the exact authorized root that
    supplied them.  It contains no Persona body, credential, or connector account data.
    """

    source_identity = _persona_source_identity(
        persona,
        settings,
        project_roots=project_roots,
    )
    connector_summary: list[PersonaConnectorCapability] = [
        {
            "kind": kind,
            "capabilities": list(get_connector_descriptor(kind).capabilities),
        }
        for kind in persona.recommended_connectors
    ]
    capability_summary: PersonaCapabilitySummary = {
        "default_approval_mode": persona.default_approval_mode,
        "recommended_work_mode": persona.recommended_work_mode,
        "recommended_connectors": connector_summary,
    }
    semantic_payload: dict[str, Any] = {
        "name": persona.name,
        "label": persona.label,
        "description": persona.description,
        "system_block": persona.system_block,
        "tool_patterns": list(persona.tool_patterns),
        "origin": persona.origin,
        "source_identity": source_identity,
        "capability_summary": capability_summary,
    }
    member_snapshots: list[ExpertTeamMemberSnapshot] = []
    if persona.expert_type == "team":
        for member in persona.team_members:
            member_payload = {
                "profile": member.profile,
                "label": member.label,
                "role": member.role,
                "reason": member.reason,
                "system_block": member.system_block,
                "tool_patterns": list(member.tool_patterns),
            }
            member_digest = hashlib.sha256(
                json.dumps(
                    member_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            member_snapshots.append(
                {
                    "profile": member.profile,
                    "label": member.label,
                    "role": member.role,
                    "reason": member.reason,
                    "tool_patterns": list(member.tool_patterns),
                    "sha256": member_digest,
                }
            )
        semantic_payload["expert_type"] = "team"
        semantic_payload["team_members"] = [
            {
                **snapshot,
                "system_block": member.system_block,
            }
            for snapshot, member in zip(member_snapshots, persona.team_members, strict=True)
        ]
    digest = hashlib.sha256(
        json.dumps(
            semantic_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    snapshot: PersonaSnapshot = {
        "schema_version": PERSONA_SNAPSHOT_SCHEMA,
        "name": persona.name,
        "origin": persona.origin,
        "source_identity": source_identity,
        "sha256": digest,
        "tool_patterns": list(persona.tool_patterns),
        "capability_summary": capability_summary,
    }
    if persona.expert_type == "team":
        snapshot["expert_type"] = "team"
        snapshot["team_members"] = member_snapshots
    return snapshot


def render_persona_system_block(
    persona: PersonaDefinition,
    snapshot: PersonaSnapshot,
) -> str:
    """渲染 run 内冻结的 Persona 指令；专家团额外携带可校验 manifest receipt。"""

    if persona.expert_type != "team":
        return persona.system_block
    if (
        snapshot.get("expert_type") != "team"
        or snapshot.get("name") != persona.name
        or not snapshot.get("team_members")
    ):
        raise ValueError(PERSONA_RESELECTION_REQUIRED)
    manifest = {
        "expert": persona.name,
        "manifest_sha256": snapshot["sha256"],
        "members": snapshot.get("team_members", []),
    }
    return (
        f"{persona.system_block}\n"
        "<expert_team_manifest>\n"
        f"{json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}\n"
        "</expert_team_manifest>\n"
        "调用 propose_team 时必须使用本 manifest 的 expert 和 manifest_sha256（参数名为 "
        "expert_sha256），成员只提交 name、profile 和可选 reason，不得自行填写 role。"
    )


def _persona_source_identity(
    persona: PersonaDefinition,
    settings: Settings,
    *,
    project_roots: tuple[Path, ...],
) -> str:
    if persona.origin == "builtin":
        if persona.source_path is not None:
            raise ValueError(PERSONA_RESELECTION_REQUIRED)
        return f"builtin:{persona.name}"

    if persona.source_path is None:
        raise ValueError(PERSONA_RESELECTION_REQUIRED)
    source = persona.source_path.expanduser().resolve()
    expected_relative = Path(f"{persona.name}.toml")
    if persona.origin == "user":
        root = (settings.cowork_data_path.expanduser() / "personas").resolve()
        if source != root / expected_relative:
            raise ValueError(PERSONA_RESELECTION_REQUIRED)
        return f"user:{root}:{expected_relative.as_posix()}"

    for raw_root in project_roots:
        root = raw_root.expanduser().resolve()
        relative = PROJECT_PERSONAS_RELATIVE / expected_relative
        if source == root / relative:
            return f"project:{root}:{relative.as_posix()}"
    raise ValueError(PERSONA_RESELECTION_REQUIRED)


def approval_mode_for_persona_change(
    *,
    current_name: str,
    requested_mode: ApprovalMode,
    selected: PersonaDefinition,
) -> ApprovalMode:
    """只在真的切 Persona 时应用默认档；换模型等普通 runtime 更新必须原样保留。"""

    return selected.default_approval_mode if selected.name != current_name else requested_mode


__all__ = [
    "PERSONA_RESELECTION_REQUIRED",
    "PROJECT_PERSONAS_RELATIVE",
    "ExpertTeamMemberDefinition",
    "ExpertTeamMemberSnapshot",
    "ExpertType",
    "PersonaCatalog",
    "PersonaDefinition",
    "PersonaSnapshot",
    "approval_mode_for_persona_change",
    "load_persona_catalog",
    "render_persona_system_block",
    "snapshot_persona",
    "tool_name_matches",
]
