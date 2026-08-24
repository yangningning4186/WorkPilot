from pathlib import Path

from starlette.responses import Response

from app.api.cowork import _preview_content_disposition


def test_preview_content_disposition_supports_chinese_filename() -> None:
    value = _preview_content_disposition(Path("MCP协议总结报告.pdf"))

    response = Response(headers={"Content-Disposition": value})

    assert response.headers["content-disposition"].startswith('inline; filename="MCP______.pdf"')
    assert "filename*=UTF-8''MCP%E5%8D%8F%E8%AE%AE" in response.headers["content-disposition"]
