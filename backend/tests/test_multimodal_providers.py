import base64
from collections.abc import Callable
from pathlib import Path

import pytest

from workpilot_ai.providers.anthropic import _anthropic_messages, _anthropic_stop_reason
from workpilot_ai.providers.gemini import _gemini_contents, _gemini_stop_reason
from workpilot_ai.providers.openai_compatible import (
    OpenAICompatibleProvider,
    _openai_stop_reason,
)
from workpilot_ai.types import Message, MessageAttachment


def _attachments(tmp_path: Path) -> tuple[MessageAttachment, ...]:
    image = tmp_path / "pixel.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    pdf = tmp_path / "brief.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    return (
        MessageAttachment(
            kind="image",
            filename="pixel.png",
            media_type="image/png",
            path=str(image),
            size_bytes=image.stat().st_size,
            sha256="a" * 64,
        ),
        MessageAttachment(
            kind="pdf",
            filename="brief.pdf",
            media_type="application/pdf",
            path=str(pdf),
            size_bytes=pdf.stat().st_size,
            sha256="b" * 64,
            extracted_text="PDF 文本",
        ),
    )


@pytest.mark.asyncio
async def test_openai_compatible_uses_image_block_and_pdf_text(tmp_path: Path) -> None:
    payload = await OpenAICompatibleProvider._message_payload(
        Message(role="user", content="分析", attachments=_attachments(tmp_path))
    )
    assert isinstance(payload["content"], list)
    assert payload["content"][1]["type"] == "image_url"
    assert payload["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "PDF 文本" in payload["content"][2]["text"]


@pytest.mark.asyncio
async def test_anthropic_and_gemini_keep_native_pdf(tmp_path: Path) -> None:
    message = Message(role="user", content="分析", attachments=_attachments(tmp_path))
    _, anthropic = await _anthropic_messages([message])
    blocks = anthropic[0]["content"]
    assert [block["type"] for block in blocks] == ["text", "image", "document"]
    assert base64.b64decode(blocks[2]["source"]["data"]).startswith(b"%PDF")

    _, gemini = await _gemini_contents([message])
    parts = gemini[0]["parts"]
    assert parts[1]["inlineData"]["mimeType"] == "image/png"
    assert parts[2]["inlineData"]["mimeType"] == "application/pdf"


@pytest.mark.parametrize(
    ("normalize", "native_reason"),
    [
        (_anthropic_stop_reason, "max_tokens"),
        (_openai_stop_reason, "length"),
        (_gemini_stop_reason, "MAX_TOKENS"),
    ],
)
def test_provider_adapters_preserve_output_limit_as_length(
    normalize: Callable[..., str], native_reason: str
) -> None:
    assert normalize(native_reason, has_tool_calls=True) == "length"
