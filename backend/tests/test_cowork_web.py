from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.cowork import web as cowork_web
from app.cowork.web import (
    CoworkWebError,
    _normalized_url,
    _pinned_request,
    fetch_url,
    search_web,
)


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
    authorized: list[str] = []

    async def public_target(url: str) -> tuple[str, ...]:
        checked.append(url)
        return ("93.184.216.34",)

    monkeypatch.setattr(cowork_web, "_assert_public_target", public_target)

    async def authorize_target(url: str) -> None:
        authorized.append(url)

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
            authorize_target=authorize_target,
        )

    assert checked == ["https://example.com/start", "https://example.com/final"]
    assert authorized == checked
    assert result.final_url == "https://example.com/final"
    assert result.title == "Example Report"
    assert "Heading" in result.content and "Useful text" in result.content
    assert "hidden" not in result.content and "ignore" not in result.content


async def test_fetch_url_rejects_cross_origin_redirect_before_second_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    async def public_target(url: str) -> tuple[str, ...]:
        del url
        return ("93.184.216.34",)

    async def authorize_target(url: str) -> None:
        if "evil.example" in url:
            raise PermissionError("origin 未授权")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/exfiltrate"})

    monkeypatch.setattr(cowork_web, "_assert_public_target", public_target)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PermissionError, match="origin 未授权"):
            await fetch_url(
                "https://trusted.example/start",
                settings=Settings(),
                client=client,
                authorize_target=authorize_target,
            )

    assert len(requests) == 1


async def test_fetch_url_detects_pdf_extension_before_query_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def public_target(url: str) -> tuple[str, ...]:
        del url
        return ("93.184.216.34",)

    async def parse_pdf(content: bytes, *, settings: Settings) -> cowork_web.PdfSnapshot:
        del content, settings
        return cowork_web.PdfSnapshot(
            path=Path("remote.pdf"),
            title="Query PDF",
            parser="test",
            page_count=1,
            content="pdf content",
            truncated=False,
            quality={},
        )

    monkeypatch.setattr(cowork_web, "_assert_public_target", public_target)
    monkeypatch.setattr(cowork_web, "_parse_remote_pdf", parse_pdf)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=b"pdf",
            )
        )
    ) as client:
        result = await fetch_url(
            "https://example.com/report.pdf?download=1",
            settings=Settings(),
            client=client,
        )

    assert result.content_type == "application/pdf"
    assert result.title == "Query PDF"


async def test_web_search_extracts_and_unwraps_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def public_target(url: str) -> tuple[str, ...]:
        del url
        return ("93.184.216.34",)

    monkeypatch.setattr(cowork_web, "_assert_public_target", public_target)
    html = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Freport">Report</a>'
        '<div class="result__snippet">A concise report summary with <b>key findings</b>.</div>'
        '<a class="result-link" href="https://second.example/docs">Documentation</a>'
        '<a class="result__snippet" href="https://second.example/docs">Official documentation summary.</a>'
        '<a class="nav-link" href="https://navigation.example/next">Next</a>'
        '<a href="/html/?q=example&amp;s=30">More Results</a>'
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html)
        )
    ) as client:
        results = await search_web(
            "example report",
            max_results=5,
            settings=Settings(),
            client=client,
        )

    assert [(item.title, item.url) for item in results] == [
        ("Report", "https://example.com/report"),
        ("Documentation", "https://second.example/docs"),
    ]
    assert [item.snippet for item in results] == [
        "A concise report summary with key findings.",
        "Official documentation summary.",
    ]


async def test_fetch_url_localizes_httpx_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def public_target(url: str) -> tuple[str, ...]:
        del url
        return ("93.184.216.34",)

    monkeypatch.setattr(cowork_web, "_assert_public_target", public_target)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(
                httpx.ConnectError("English detail", request=request)
            )
        )
    ) as client:
        with pytest.raises(CoworkWebError) as captured:
            await fetch_url("https://example.com", settings=Settings(), client=client)

    assert str(captured.value) == "网页连接失败"
    assert "English" not in str(captured.value)


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
