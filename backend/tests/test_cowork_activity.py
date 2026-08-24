from app.cowork.activity import activity_description, describe_tool_activity


def test_shell_activity_keeps_intent_and_redacts_credentials() -> None:
    activity = describe_tool_activity(
        "run_shell",
        {
            "reason": "渲染 PPT 并检查页面",
            "command": "API_KEY=very-secret python render.py --input deck.pptx",
        },
    )

    assert activity == {
        "title": "执行 Shell 命令",
        "summary": "渲染 PPT 并检查页面",
        "target": "API_KEY=<已隐藏> python render.py --input deck.pptx",
        "target_kind": "code",
    }


def test_file_activity_never_persists_replacement_content() -> None:
    activity = describe_tool_activity(
        "replace_in_file",
        {
            "path": "/Users/demo/WorkPilot/generate_ai_ppt.py",
            "old_text": "private old body",
            "new_text": "private new body",
            "baseline_sha256": "a" * 64,
        },
    )

    assert activity == {
        "title": "修改文件",
        "summary": "替换文件中的指定内容",
        "target": "/Users/demo/WorkPilot/generate_ai_ppt.py",
        "target_kind": "path",
    }
    assert "private" not in activity_description(activity)


def test_unknown_tool_only_uses_allowlisted_target_fields() -> None:
    activity = describe_tool_activity(
        "mcp_custom_action",
        {
            "action": "create_record",
            "body": {"password": "do-not-log", "content": "private"},
        },
    )

    assert activity["title"] == "mcp custom action"
    assert activity["target"] == "create_record"
    assert "do-not-log" not in repr(activity)
