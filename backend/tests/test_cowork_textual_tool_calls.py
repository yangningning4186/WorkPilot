import json

import pytest

from app.cowork.textual_tool_calls import TextualToolCallError, recover_textual_tool_calls
from app.cowork.tools import build_default_cowork_registry


def test_recovers_hermes_text_call_only_after_schema_validation() -> None:
    registry = build_default_cowork_registry()

    text, calls = recover_textual_tool_calls(
        "我来查看。<tool_call><function=list_files>"
        "<parameter=path>.</parameter><parameter=recursive>true</parameter>"
        "</function></tool_call>",
        visible_tool_names={"list_files"},
        validate=registry.parse_arguments,
        id_prefix="turn-1",
    )

    assert text == "我来查看。"
    assert len(calls) == 1 and calls[0].name == "list_files"
    assert json.loads(calls[0].arguments)["recursive"] is True


def test_recovers_json_text_call_with_canonical_arguments() -> None:
    registry = build_default_cowork_registry()

    _, calls = recover_textual_tool_calls(
        '<tool_call>{"name":"web_search","arguments":{"query":"AI news"}}</tool_call>',
        visible_tool_names={"web_search"},
        validate=registry.parse_arguments,
        id_prefix="turn-2",
    )

    assert json.loads(calls[0].arguments) == {"query": "AI news", "max_results": 8}


def test_recovery_rejects_tools_whose_schema_was_not_exposed_this_round() -> None:
    registry = build_default_cowork_registry()

    with pytest.raises(TextualToolCallError, match="schema 本轮未下发"):
        recover_textual_tool_calls(
            '<tool_call>{"name":"run_shell","arguments":{"command":"pwd"}}</tool_call>',
            visible_tool_names={"list_files"},
            validate=registry.parse_arguments,
            id_prefix="turn-3",
        )


def test_recovery_rejects_arguments_that_fail_the_registered_schema() -> None:
    registry = build_default_cowork_registry()

    with pytest.raises(TextualToolCallError, match="参数不符合 schema"):
        recover_textual_tool_calls(
            '<tool_call>{"name":"web_search","arguments":{"query":""}}</tool_call>',
            visible_tool_names={"web_search"},
            validate=registry.parse_arguments,
            id_prefix="turn-4",
        )
