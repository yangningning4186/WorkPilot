"""常驻审批规则：把"每一次都问"降级成"这一类问过一次"。

逐次审批是安全的下限，不是终点。真实用法里它有两个具体代价：

- **无人值守跑批必然停住。** 一条每天早上七点跑的计划，如果每次都要人点一下"允许
  `npm test`"，那它就不是无人值守。
- **同一条命令被问二十遍。** 用户第三次点"允许"之后，第四次弹窗提供的已经不是安全，
  而是疲劳——疲劳会让人开始不看内容就点允许，那才是真正的风险。

所以这里给出三种匹配粒度，全部由**用户**在审批那一刻选择，模型无权创建规则：

- ``tool``：这只工具的任何调用（`ALWAYS_TOOL`）。
- ``target``：精确目标一致才算数，比如同一个连接器的同一个 API path。
- ``command_prefix``：`run_shell` 专用，argv 前缀命中且命令里没有 shell 操作符
  （`|`、`&&`、`;`、重定向……）。少了后半个条件，`npm test` 的授权就能被
  `npm test && rm -rf ~` 白嫖走。

**规则不放大能力。** capability 闸门在注册表入口，规则只作用于"要不要再问一次人"。
没有 `shell.execute` 的会话，攒再多规则也跑不了命令。
"""

from __future__ import annotations

import json
import shlex
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

# 一条 argv 前缀最多记这么多个词。再长就不是"这一类命令"而是"这一条命令"了，
# 而那种情况本来就该逐次审批。
MAX_COMMAND_PREFIX_WORDS = 4


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


def command_prefix(argv: Sequence[str], *, words: int = 2) -> str:
    """把一条命令收敛成可复用的 argv 前缀。

    用 `shlex.join` 而不是空格拼接：`git commit -m "两个词"` 直接拼出来的字符串再被
    `shlex.split` 解析回去就不是同一条命令了，规则会静默错配。
    """

    if not argv:
        raise ApprovalRuleError("命令为空，无法生成常驻规则")
    limit = max(1, min(words, MAX_COMMAND_PREFIX_WORDS))
    return shlex.join(list(argv[:limit]))


def command_matches_prefix(argv: Sequence[str], prefix: str, *, has_operators: bool) -> bool:
    """argv 前缀匹配。

    `has_operators` 为真时一律不匹配：一条允许过的命令加上 `&&` 就变成了两条，而后一条
    从没被任何人看过。这与部署级 allowlist 用的是同一条不变量。
    """

    if has_operators:
        return False
    try:
        expected = shlex.split(prefix)
    except ValueError:
        return False
    if not expected or len(argv) < len(expected):
        return False
    return list(argv[: len(expected)]) == expected


def call_target(tool: str, arguments: Mapping[str, Any], *, fields: Sequence[str]) -> str:
    """一次调用的规范化目标串。

    只取工具自己声明的那几个"决定后果落在哪里"的参数——连接器的 account + method +
    path、上传的目标文件。正文（`body`、文件内容）**不**进目标：把它算进去等于每次调用
    都是一个新目标，规则永远匹配不上；而把它排除掉的代价是明确的，所以 `target` 粒度
    只适合那些"目标定了、后果就定了"的工具。
    """

    payload = {field: arguments.get(field) for field in sorted(fields)}
    return json.dumps(
        {"tool": tool, "target": payload},
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
    if match_kind == "tool":
        target = None
    elif not (target or "").strip():
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
    return await store.revoke_approval_rule(
        conversation_id=conversation_id, rule_id=rule_id
    )


async def find_matching_rule(
    session: AsyncSession,
    *,
    conversation_id: UUID,
    schedule_id: UUID | None,
    tool: str,
    target: str | None = None,
    argv: Sequence[str] | None = None,
    has_operators: bool = False,
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
        if rule.match_kind == "tool":
            return rule
        if rule.match_kind == "target" and target is not None and rule.target == target:
            return rule
        if (
            rule.match_kind == "command_prefix"
            and argv is not None
            and rule.target is not None
            and command_matches_prefix(argv, rule.target, has_operators=has_operators)
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
