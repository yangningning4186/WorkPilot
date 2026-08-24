"""常驻审批规则。

逐次审批是安全的下限，但它有两个具体代价：无人值守计划每次都会停在审批上，以及同一条
命令被问二十遍之后，弹窗提供的不再是安全而是疲劳。这套规则把"每一次都问"降级成
"这一类问过一次"，而这里锁住的是它**不能**顺带放开的东西。
"""

from uuid import UUID, uuid4

import pytest

from app.core.db import DbSession as AsyncSession
from app.cowork.approvals import (
    ApprovalRuleError,
    action_target,
    argv_matches_pattern,
    argv_pattern,
    conversation_approval_mode,
    create_approval_rule,
    find_matching_rule,
    list_approval_rules,
    revoke_approval_rule,
)
from app.cowork.interactions import resolve_inbox_item
from app.cowork.shell import assess_shell_command
from app.cowork_contracts import InboxRecord
from app.runstore.checkpoints import ensure_plan
from app.runstore.conversations import update_conversation_runtime
from app.runstore.runs import ensure_conversation


def test_argv_pattern_round_trips_exact_arguments_and_cwd() -> None:
    """完整 argv 按 JSON 保存，不再经过字符串拆词或前缀匹配。"""

    argv = ("git", "commit", "-m", "两 个 词")
    pattern = argv_pattern(argv, cwd="/workspace")
    assert argv_matches_pattern(argv, pattern, has_operators=False, cwd="/workspace") is True
    assert argv_matches_pattern(argv, pattern, has_operators=False, cwd="/other") is False


def test_a_prefix_never_authorises_a_chained_command() -> None:
    """`npm test` 的授权不能被 `npm test && rm -rf ~` 白嫖走。

    这是整套规则里最重要的一条不变量：命令里出现 shell 操作符，就意味着后面还挂着一条
    从没被任何人看过的命令。
    """

    chained = assess_shell_command("npm test && rm -rf ~", []).command
    assert chained.has_operators is True
    assert (
        argv_matches_pattern(
            chained.argv,
            argv_pattern(chained.argv, cwd="/workspace"),
            has_operators=chained.has_operators,
            cwd="/workspace",
        )
        is False
    )


def test_argv_pattern_rejects_appended_or_mutated_arguments() -> None:
    """批准后的参数增加、删除或替换都不能命中。"""

    pattern = argv_pattern(("npm", "test"), cwd="/workspace")
    assert (
        argv_matches_pattern(("npm", "test"), pattern, has_operators=False, cwd="/workspace")
        is True
    )
    assert (
        argv_matches_pattern(
            ("npm", "test", "--watch"),
            pattern,
            has_operators=False,
            cwd="/workspace",
        )
        is False
    )
    assert (
        argv_matches_pattern(("npm", "publish"), pattern, has_operators=False, cwd="/workspace")
        is False
    )


def test_action_target_is_stable_and_separates_delete_from_write() -> None:
    """目标只取"后果落在哪里"的那几个字段。

    把 body 算进去等于每次调用都是新目标，规则永远匹配不上；把它排除掉的代价是明确的，
    所以只有声明了目标字段的工具才提供 target 粒度。
    """

    fields = ("account_id", "method", "path")
    first = action_target(
        "act_connector_api",
        {"account_id": "a", "method": "POST", "path": "/issues", "body": {"title": "x"}},
        fields=fields,
    )
    second = action_target(
        "act_connector_api",
        {"path": "/issues", "method": "POST", "account_id": "a", "body": {"title": "y"}},
        fields=fields,
    )
    assert first == second
    third = action_target(
        "act_connector_api",
        {"account_id": "a", "method": "DELETE", "path": "/issues"},
        fields=fields,
    )
    assert third != first


def test_new_rule_kinds_require_a_target() -> None:
    with pytest.raises(ApprovalRuleError, match="必须带目标"):
        import asyncio

        asyncio.run(
            create_approval_rule(
                None,  # type: ignore[arg-type]
                conversation_id=uuid4(),
                tool="run_shell",
                match_kind="argv_pattern",
                target="   ",
            )
        )


@pytest.mark.integration
async def test_rules_match_by_kind_and_stop_matching_once_revoked(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Approval rules")
    pattern = argv_pattern(("npm", "test"), cwd="/workspace")
    exact_rule = await create_approval_rule(
        db_session,
        conversation_id=conversation_id,
        tool="run_shell",
        match_kind="argv_pattern",
        target=pattern,
    )
    await db_session.commit()

    assert (
        await find_matching_rule(
            db_session,
            conversation_id=conversation_id,
            schedule_id=None,
            tool="run_shell",
            argv=("npm", "test"),
            cwd="/workspace",
        )
    ) is not None
    # 参数追加不该沾光。
    assert (
        await find_matching_rule(
            db_session,
            conversation_id=conversation_id,
            schedule_id=None,
            tool="run_shell",
            argv=("npm", "test", "--watch"),
            cwd="/workspace",
        )
    ) is None

    assert await revoke_approval_rule(
        db_session, conversation_id=conversation_id, rule_id=exact_rule.id
    )
    await db_session.commit()
    assert (
        await find_matching_rule(
            db_session,
            conversation_id=conversation_id,
            schedule_id=None,
            tool="run_shell",
            argv=("npm", "test"),
            cwd="/workspace",
        )
    ) is None


@pytest.mark.integration
async def test_schedule_scoped_rules_do_not_leak_into_manual_runs(
    db_session: AsyncSession,
) -> None:
    """计划攒下的免审批授权不能被用户手工发起的对话继承。

    做成会话级会让手工对话悄悄拿到一批自己从没看过的授权——那是"以为只给了那条计划"
    的权限。
    """

    conversation_id = await ensure_conversation(db_session, title="Schedule scope")
    schedule_id = await _seed_schedule(db_session, conversation_id=conversation_id)
    await create_approval_rule(
        db_session,
        conversation_id=conversation_id,
        tool="run_shell",
        match_kind="argv_pattern",
        target=argv_pattern(("npm", "test"), cwd="/workspace"),
        scope="schedule",
        schedule_id=schedule_id,
        created_by="schedule",
    )
    await db_session.commit()

    assert (
        await find_matching_rule(
            db_session,
            conversation_id=conversation_id,
            schedule_id=schedule_id,
            tool="run_shell",
            argv=("npm", "test"),
            cwd="/workspace",
        )
    ) is not None
    assert (
        await find_matching_rule(
            db_session,
            conversation_id=conversation_id,
            schedule_id=None,
            tool="run_shell",
            argv=("npm", "test"),
        )
    ) is None


@pytest.mark.integration
async def test_approving_with_remember_records_exactly_what_the_card_showed(
    db_session: AsyncSession,
) -> None:
    """规则必须从 inbox 里那份 payload 派生。

    那份 payload 就是用户在卡片上看到的内容。改成事后从模型输入重算，用户点的和最终
    生效的就可能不是同一条规则。
    """

    conversation_id = await ensure_conversation(db_session, title="Remember")
    item = await _pending_shell_item(
        db_session,
        conversation_id=conversation_id,
        request={
            "command": "npm test",
            "argv": ["npm", "test"],
            "has_operators": False,
            "standing_argv_pattern": argv_pattern(("npm", "test"), cwd="/workspace"),
        },
    )
    _, response = await resolve_inbox_item(db_session, item=item, approved=True, remember="command")
    await db_session.commit()

    assert response["standing_rule"]["match_kind"] == "argv_pattern"
    assert response["standing_rule"]["target"] == argv_pattern(("npm", "test"), cwd="/workspace")
    rules = await list_approval_rules(db_session, conversation_id=conversation_id)
    assert [rule.target for rule in rules] == [argv_pattern(("npm", "test"), cwd="/workspace")]


@pytest.mark.integration
async def test_a_command_with_operators_cannot_be_remembered(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Operators")
    item = await _pending_shell_item(
        db_session,
        conversation_id=conversation_id,
        request={
            "command": "npm test && rm -rf ~",
            "argv": ["npm", "test"],
            "has_operators": True,
            "standing_argv_pattern": None,
        },
    )
    with pytest.raises(ValueError, match="shell 操作符"):
        await resolve_inbox_item(db_session, item=item, approved=True, remember="command")


@pytest.mark.integration
async def test_approval_mode_defaults_to_interactive_and_flips_explicitly(
    db_session: AsyncSession,
) -> None:
    conversation_id = await ensure_conversation(db_session, title="Approval mode")
    assert (
        await conversation_approval_mode(db_session, conversation_id=conversation_id)
        == "interactive"
    )
    await update_conversation_runtime(
        db_session,
        conversation_id=conversation_id,
        provider_profile_id=None,
        model_override=None,
        unattended=False,
        approval_mode="auto",
    )
    await db_session.commit()
    assert await conversation_approval_mode(db_session, conversation_id=conversation_id) == "auto"


async def _seed_schedule(session: AsyncSession, *, conversation_id: UUID) -> UUID:
    from app.cowork.schedules import create_schedule

    created = await create_schedule(
        session,
        conversation_id=conversation_id,
        title="每日检查",
        goal="跑一次测试",
        schedule_kind="cron",
        cron_expression="0 7 * * *",
        run_at=None,
        timezone="Asia/Shanghai",
    )
    await session.commit()
    return created.id


async def _pending_shell_item(
    session: AsyncSession, *, conversation_id: UUID, request: dict
) -> InboxRecord:
    from app.cowork.interactions import create_inbox_item
    from app.runstore.runs import create_run

    run = await create_run(
        session,
        conversation_id=conversation_id,
        goal="approval",
        workflow_type="cowork",
        budget_tokens=10_000,
        budget_calls=10,
        budget_wall_ms=60_000,
    )
    step_id = uuid4()
    await ensure_plan(
        session,
        run_id=run.id,
        steps=[
            {
                "id": str(step_id),
                "idx": 0,
                "description": "等待用户批准 shell 命令",
                "tool": "run_shell",
                "depends_on": [],
                "status": "running",
            }
        ],
    )
    item = await create_inbox_item(
        session,
        run_id=run.id,
        conversation_id=conversation_id,
        kind="shell_approval",
        tool_call_id="call-1",
        plan_step_id=step_id,
        request=request,
    )
    await session.commit()
    return item
