"""常驻审批规则：把"每一次都问"降级成"这一类问过一次"。

逐次审批是安全的下限，不是终点。真实用法里它有两个具体代价：

- **无人值守跑批必然停住。** 一条每天早上七点跑的计划，如果每次都要人点一下"允许
  `npm test`"，那它就不是无人值守。
- **同一条命令被问二十遍。** 用户第三次点"允许"之后，第四次弹窗提供的已经不是安全，
  而是疲劳——疲劳会让人开始不看内容就点允许，那才是真正的风险。

所以新规则只有两种匹配粒度，全部由**用户**在审批那一刻选择，模型无权创建规则：

- ``action_target``：动作和目标的规范化 JSON 必须同时精确一致。
- ``argv_pattern``：`run_shell` 的完整 argv（及可选 cwd）必须精确一致，且命令不能带
  shell 操作符。它叫 pattern 是为了协议可演进；v1 刻意没有通配符和前缀语义。

**规则不放大能力。** capability 闸门在注册表入口，规则只作用于"要不要再问一次人"。
没有 `host.execute` 的会话，攒再多规则也跑不了命令。历史 ``tool`` / ``target`` /
``command_prefix`` 记录只允许查看和撤销，匹配器对它们 fail closed。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.db import DbSession as AsyncSession
from app.cowork_contracts import (
    ApprovalMatchKind,
    ApprovalMode,
    ApprovalRuleRecord,
    ApprovalRuleScope,
)
from app.cowork_store.routing import cowork_store

_COLUMNS = """
    id, conversation_id, scope, schedule_id, tool, match_kind, target,
    created_by, revoked_at, created_at
"""


class ApprovalRuleError(ValueError):
    """面向用户与模型的规则错误。"""


def _record(row: Any) -> ApprovalRuleRecord:
    return ApprovalRuleRecord(
        id=row["id"] if isinstance(row["id"], UUID) else UUID(str(row["id"])),
        conversation_id=(
            row["conversation_id"]
            if isinstance(row["conversation_id"], UUID)
            else UUID(str(row["conversation_id"]))
        ),
        scope=row["scope"],
        schedule_id=(
            None
            if row["schedule_id"] is None
            else (
                row["schedule_id"]
                if isinstance(row["schedule_id"], UUID)
                else UUID(str(row["schedule_id"]))
            )
        ),
        tool=row["tool"],
        match_kind=row["match_kind"],
        target=row["target"],
        created_by=row["created_by"],
        revoked_at=_as_datetime(row["revoked_at"]),
        created_at=_as_datetime(row["created_at"]),
    )


def _as_datetime(value: Any) -> Any:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def argv_pattern(argv: Sequence[str], *, cwd: str) -> str:
    """生成 v1 完整 argv 模式；没有通配符、前缀或 shell 字符串再解析。"""

    if not argv:
        raise ApprovalRuleError("命令为空，无法生成常驻规则")
    if len(argv) > 256 or any(not item or len(item) > 4096 for item in argv):
        raise ApprovalRuleError("命令参数过多、为空或单个参数过长")
    if not cwd or not cwd.startswith("/") or any(token in cwd for token in ("\x00", "\n", "\r")):
        raise ApprovalRuleError("常驻命令规则必须绑定规范化绝对 cwd")
    return json.dumps(
        {"version": 1, "argv": list(argv), "cwd": cwd},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def argv_matches_pattern(
    argv: Sequence[str], pattern: str, *, has_operators: bool, cwd: str | None = None
) -> bool:
    if has_operators:
        return False
    try:
        expected = json.loads(pattern)
    except (TypeError, ValueError):
        return False
    if not isinstance(expected, dict) or expected.get("version") != 1:
        return False
    expected_argv = expected.get("argv")
    expected_cwd = expected.get("cwd")
    return (
        isinstance(expected_argv, list)
        and all(isinstance(item, str) for item in expected_argv)
        and list(argv) == expected_argv
        and isinstance(expected_cwd, str)
        and expected_cwd == cwd
    )


def action_target(tool: str, arguments: Mapping[str, Any], *, fields: Sequence[str]) -> str:
    """一次调用的规范化 action + target 串。

    只取工具自己声明的那几个"决定后果落在哪里"的参数——连接器的 account + method +
    path、上传的目标文件。正文（`body`、文件内容）**不**进目标：把它算进去等于每次调用
    都是一个新目标，规则永远匹配不上；而把它排除掉的代价是明确的，所以 `target` 粒度
    只适合那些"目标定了、后果就定了"的工具。
    """

    action_field = (
        "action" if "action" in arguments else "method" if "method" in arguments else None
    )
    action = arguments.get(action_field) if action_field is not None else tool
    payload = {
        field: arguments.get(field) for field in sorted(fields) if field not in {"action", "method"}
    }
    return json.dumps(
        {"version": 1, "tool": tool, "action": action, "target": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def create_approval_rule(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    tool: str,
    match_kind: ApprovalMatchKind,
    target: str | None,
    scope: ApprovalRuleScope = "conversation",
    schedule_id: UUID | None = None,
    created_by: str = "user",
) -> ApprovalRuleRecord:
    if match_kind not in {"action_target", "argv_pattern"}:
        raise ApprovalRuleError(f"历史规则类型 {match_kind} 已停用，不能新建")
    if not (target or "").strip():
        raise ApprovalRuleError(f"{match_kind} 规则必须带目标")
    if (scope == "schedule") != (schedule_id is not None):
        raise ApprovalRuleError("scope=schedule 必须且只能带 schedule_id")

    store = cowork_store()
    return await store.create_approval_rule(
        conversation_id=conversation_id,
        tool=tool,
        match_kind=match_kind,
        target=target,
        scope=scope,
        schedule_id=schedule_id,
        created_by=created_by,
    )


async def list_approval_rules(
    session: AsyncSession, *, conversation_id: UUID, include_revoked: bool = False
) -> list[ApprovalRuleRecord]:
    store = cowork_store()
    return await store.list_approval_rules(
        conversation_id=conversation_id, include_revoked=include_revoked
    )


async def revoke_approval_rule(
    session: AsyncSession, *, conversation_id: UUID, rule_id: UUID
) -> bool:
    store = cowork_store()
    return await store.revoke_approval_rule(conversation_id=conversation_id, rule_id=rule_id)


async def find_matching_rule(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    schedule_id: UUID | None,
    tool: str,
    target: str | None = None,
    argv: Sequence[str] | None = None,
    has_operators: bool = False,
    cwd: str | None = None,
) -> ApprovalRuleRecord | None:
    """这次调用有没有被某条常驻规则覆盖。

    计划派生的规则只对同一个 schedule 的运行有效：把它们做成会话级会让用户手工发起的
    对话悄悄继承一批自己从没看过的授权。
    """

    for rule in await list_approval_rules(session, conversation_id=conversation_id):
        if rule.tool != tool:
            continue
        if rule.scope == "schedule" and rule.schedule_id != schedule_id:
            continue
        if rule.match_kind == "action_target" and target is not None and rule.target == target:
            return rule
        if (
            rule.match_kind == "argv_pattern"
            and argv is not None
            and rule.target is not None
            and argv_matches_pattern(
                argv,
                rule.target,
                has_operators=has_operators,
                cwd=cwd,
            )
        ):
            return rule
    return None


async def conversation_approval_mode(
    session: AsyncSession, *, conversation_id: UUID
) -> ApprovalMode:
    """会话当前的自主权上限。

    每次闸门都重新读，不缓存进 checkpoint：用户在 run 跑到一半把免审批关掉，应该当场
    生效。反方向（跑到一半打开）本来就是用户的显式动作，不需要额外保护。
    """

    store = cowork_store()
    rows = await store.list_conversation_metadata(
        conversation_id=conversation_id, archived=None, limit=1
    )
    if not rows:
        return "interactive"
    value = rows[0].get("approval_mode")
    return "auto" if value == "auto" else "interactive"
