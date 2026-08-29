from __future__ import annotations

from app.agent_core.messages import (
    compaction_summary,
    convert_to_llm,
    convert_to_llm_messages,
    runtime_directive,
)
from workpilot_ai.types import TextContentBlock, ThinkingContentBlock


def test_runtime_directive_is_canonical_custom_role_and_flattens_once() -> None:
    directive = runtime_directive("只依据已登记证据修复答案", source="citation_repair")

    assert directive["role"] == "runtime_directive"
    converted = convert_to_llm(directive)

    assert converted is not None
    assert converted.role == "user"
    assert converted.content == (
        '<runtime_directive source="citation_repair">\n'
        "只依据已登记证据修复答案\n"
        "</runtime_directive>"
    )


def test_ui_only_custom_message_is_filtered_from_provider_history() -> None:
    messages = convert_to_llm_messages(
        [
            {"role": "user", "content": "任务"},
            {"role": "custom", "kind": "ui_status", "content": "正在同步"},
            {"role": "assistant", "content": "完成"},
        ]
    )

    assert [(message.role, message.content) for message in messages] == [
        ("user", "任务"),
        ("assistant", "完成"),
    ]


def test_compaction_summary_has_agent_role_but_provider_user_shape() -> None:
    summary = compaction_summary("<summary>较早历史</summary>")

    assert summary["role"] == "compaction_summary"
    converted = convert_to_llm(summary)

    assert converted is not None
    assert converted.role == "user"
    assert converted.content == "<summary>较早历史</summary>"


def test_assistant_signed_thinking_blocks_survive_provider_conversion() -> None:
    converted = convert_to_llm(
        {
            "role": "assistant",
            "content": "开始调用工具",
            "content_blocks": [
                {
                    "type": "thinking",
                    "thinking": "内部推理",
                    "signature": "signed-payload",
                },
                {"type": "text", "text": "开始调用工具"},
            ],
        }
    )

    assert converted is not None
    assert converted.content_blocks == (
        ThinkingContentBlock(thinking="内部推理", signature="signed-payload"),
        TextContentBlock(text="开始调用工具"),
    )
