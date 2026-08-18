from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.services import cowork_web
from app.services.cowork_web import CoworkWebError, _normalized_url, _pinned_request, fetch_url


def test_web_url_validation_rejects_credentials_localhost_and_non_http() -> None:
    assert _normalized_url("HTTPS://Example.com/path#fragment") == "https://example.com/path"
    with pytest.raises(CoworkWebError, match="http/https"):
        _normalized_url("file:///etc/passwd")
    with pytest.raises(CoworkWebError, match="用户信息"):
        _normalized_url("https://user:pass@example.com/")
    with pytest.raises(CoworkWebError, match="私有网络"):
        _normalized_url("http://localhost:8000/")
    assert _pinned_request("https://example.com:8443/path?q=1", "93.184.216.34") == (
        "https://93.184.216.34:8443/path?q=1",
        "example.com:8443",
        "example.com",
    )


async def test_fetch_url_extracts_readable_html_and_revalidates_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked: list[str] = []

    async def public_target(url: str) -> tuple[str, ...]:
        checked.append(url)
        return ("93.184.216.34",)

    monkeypatch.setattr(cowork_web, "_assert_public_target", public_target)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><head><title> Example Report </title><style>hidden</style></head>"
                "<body><h1>Heading</h1><p>Useful text</p><script>ignore()</script></body></html>"
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_url(
            "https://example.com/start",
            settings=Settings(cowork_web_text_max_chars=1000),
            client=client,
        )

    assert checked == ["https://example.com/start", "https://example.com/final"]
    assert result.final_url == "https://example.com/final"
    assert result.title == "Example Report"
    assert "Heading" in result.content and "Useful text" in result.content
    assert "hidden" not in result.content and "ignore" not in result.content


async def test_fetch_url_rejects_oversized_and_unsupported_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del tmp_path

    async def public_target(url: str) -> tuple[str, ...]:
        del url
        return ("93.184.216.34",)

    monkeypatch.setattr(cowork_web, "_assert_public_target", public_target)
    settings = Settings(cowork_web_max_bytes=1024)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "application/octet-stream"}, content=b"data"
            )
        )
    ) as client:
        with pytest.raises(CoworkWebError, match="Content-Type"):
            await fetch_url("https://example.com/data", settings=settings, client=client)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"x" * 1025
            )
        )
    ) as client:
        with pytest.raises(CoworkWebError, match="1024 bytes"):
            await fetch_url("https://example.com/large", settings=settings, client=client)
