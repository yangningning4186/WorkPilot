from app.cowork.conversation_titles import (
    fallback_conversation_title,
    generate_conversation_title,
    is_placeholder_title,
    sanitize_generated_title,
)
from tests.fakes import DeterministicProvider
from workpilot_ai.gateway import ModelGateway


def test_fallback_title_summarizes_common_cowork_goals() -> None:
    assert (
        fallback_conversation_title("这篇论文的方法部分具体怎么做的？按步骤讲，并引用原文标出处。")
        == "解读论文方法与步骤"
    )
    assert (
        fallback_conversation_title(
            "使用已连接的 GitHub 账户，在 https://github.com/acme/repo 创建一个测试 Issue"
        )
        == "创建 GitHub 测试 Issue"
    )
    assert fallback_conversation_title("hello") == "简单问候"
    assert (
        fallback_conversation_title("帮我整理今天最热的 5 条 AI 资讯，并生成 PPT")
        == "制作今日 AI 资讯 PPT"
    )
    assert fallback_conversation_title("总结一下这篇 PDF 文档") == "总结 PDF 文档"
    assert fallback_conversation_title("调查 Nvidia 显卡的架构") == "调研 Nvidia GPU 架构"


def test_placeholder_and_generated_title_sanitizing() -> None:
    assert is_placeholder_title("Cowork 35")
    assert is_placeholder_title("新会话")
    assert not is_placeholder_title("解读论文方法")
    assert sanitize_generated_title("**标题：** “论文方法与证据链。”") == "论文方法与证据链"
    assert sanitize_generated_title("<think>分析</think>\n论文评测可信度") == "论文评测可信度"


async def test_title_generation_uses_lightweight_metadata_call() -> None:
    provider = DeterministicProvider(completion_text="论文方法与证据链")
    gateway = ModelGateway(
        provider,
        embedding_dimensions=4,
        default_context_window_tokens=32_000,
    )

    title = await generate_conversation_title(
        gateway,
        user_message="解释这篇论文的方法，并引用原文",
    )

    assert title == "论文方法与证据链"
    assert provider.last_messages[0].role == "system"
    assert "不可信数据" in provider.last_messages[0].content
