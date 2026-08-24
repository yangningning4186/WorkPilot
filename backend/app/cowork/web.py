"""Cowork 受控网页/PDF 读取：限大小、限重定向并拒绝私有网络。"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit, urlunsplit

import httpx

from app.core.config import Settings
from app.cowork.files import PdfSnapshot, read_pdf_file


class CoworkWebError(RuntimeError):
    pass


NetworkAuthorizer = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class WebSnapshot:
    url: str
    final_url: str
    title: str
    content_type: str
    content: str
    truncated: bool
    status_code: int
    pdf: PdfSnapshot | None = None
    links: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str


class _ReadableHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self._link_href: str | None = None
        self._link_parts: list[str] = []
        self._link_class = ""
        self._snippet_tag: str | None = None
        self._snippet_same_tag_depth = 0
        self._snippet_parts: list[str] = []
        self.search_snippets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.casefold()
        css_class = next((value or "" for name, value in attrs if name == "class"), "")
        classes = frozenset(css_class.split())
        if self._snippet_tag == lowered:
            self._snippet_same_tag_depth += 1
        elif self._snippet_tag is None and classes.intersection(
            {"result__snippet", "result-snippet"}
        ):
            self._snippet_tag = lowered
            self._snippet_same_tag_depth = 1
            self._snippet_parts = []
        if lowered in {"script", "style", "noscript", "svg", "canvas"}:
            self._ignored_depth += 1
        elif lowered == "title":
            self._in_title = True
        elif lowered == "a":
            self._link_href = next((value for name, value in attrs if name == "href"), None)
            self._link_class = css_class
            self._link_parts = []
        elif lowered in {"p", "div", "section", "article", "br", "li", "tr", "h1", "h2", "h3"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered in {"script", "style", "noscript", "svg", "canvas"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif lowered == "title":
            self._in_title = False
        elif lowered == "a":
            label = " ".join(" ".join(self._link_parts).split())
            if self._link_href and label:
                self.links.append(
                    {"title": label, "url": self._link_href, "class": self._link_class}
                )
            self._link_href = None
            self._link_class = ""
            self._link_parts = []
        elif lowered in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3"}:
            self.text_parts.append("\n")
        if self._snippet_tag == lowered:
            self._snippet_same_tag_depth -= 1
            if self._snippet_same_tag_depth <= 0:
                snippet = " ".join(" ".join(self._snippet_parts).split())
                snippet = re.sub(r"\s+([.,!?;:，。！？；：])", r"\1", snippet)
                if snippet:
                    self.search_snippets.append(snippet)
                self._snippet_tag = None
                self._snippet_same_tag_depth = 0
                self._snippet_parts = []

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        if self._link_href is not None:
            self._link_parts.append(data)
        if self._snippet_tag is not None:
            self._snippet_parts.append(data)
        self.text_parts.append(data)

    def result(self) -> tuple[str, str, tuple[dict[str, str], ...]]:
        title = " ".join(" ".join(self.title_parts).split())
        lines = [" ".join(line.split()) for line in "".join(self.text_parts).splitlines()]
        text = "\n".join(line for line in lines if line)
        snippets = iter(self.search_snippets)
        links: list[dict[str, str]] = []
        for link in self.links:
            item = dict(link)
            classes = frozenset(item.get("class", "").split())
            if classes.intersection({"result__a", "result-link"}):
                item["snippet"] = next(snippets, "")
            links.append(item)
        return title, text, tuple(links[:500])


def normalize_public_url(raw_url: str) -> str:
    try:
        parsed = urlsplit(raw_url.strip())
        port = parsed.port
    except ValueError as error:
        raise CoworkWebError("网址无效") from error
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise CoworkWebError("只允许 http/https 网址")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise CoworkWebError("网址必须包含不带用户信息的主机名")
    if port is not None and not 1 <= port <= 65535:  # pragma: no cover - urlsplit 已拦截
        raise CoworkWebError("网址端口无效")
    try:
        hostname = parsed.hostname.casefold().rstrip(".").encode("idna").decode("ascii")
    except UnicodeError as error:
        raise CoworkWebError("网页主机名无效") from error
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise CoworkWebError("网页工具不能访问本机或私有网络")
    netloc = hostname
    if ":" in hostname:
        netloc = f"[{hostname}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path or "/", parsed.query, ""))


def _is_public_address(address: str) -> bool:
    try:
        value = ipaddress.ip_address(address)
    except ValueError:
        return False
    return bool(value.is_global)


async def assert_public_target(url: str) -> tuple[str, ...]:
    parsed = urlsplit(url)
    assert parsed.hostname is not None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            parsed.hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise CoworkWebError(f"无法解析网页主机: {parsed.hostname}") from error
    addresses = {str(record[4][0]) for record in records}
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise CoworkWebError("网页工具不能访问本机、私有、保留或链路本地地址")
    return tuple(sorted(addresses, key=lambda item: (":" in item, item)))


# 保留内部旧名称，既兼容已有扩展/测试的 monkeypatch，也让新浏览器执行器使用
# 明确的公开名称。fetch_url 继续从别名取值，测试不会悄悄失去 DNS 隔离。
_normalized_url = normalize_public_url
_assert_public_target = assert_public_target


def _pinned_request(url: str, address: str) -> tuple[str, str, str]:
    """把已校验 IP 钉在实际连接上，避免校验后二次 DNS 解析导致 rebinding。"""

    parsed = urlsplit(url)
    assert parsed.hostname is not None
    pinned_host = f"[{address}]" if ":" in address else address
    if parsed.port is not None:
        pinned_host = f"{pinned_host}:{parsed.port}"
    request_url = urlunsplit((parsed.scheme, pinned_host, parsed.path, parsed.query, ""))
    host_header = parsed.hostname
    if ":" in host_header:
        host_header = f"[{host_header}]"
    default_port = 443 if parsed.scheme == "https" else 80
    if parsed.port is not None and parsed.port != default_port:
        host_header = f"{host_header}:{parsed.port}"
    return request_url, host_header, parsed.hostname


async def _bounded_response_body(response: httpx.Response, max_bytes: int) -> bytes:
    content = bytearray()
    async for chunk in response.aiter_bytes():
        content.extend(chunk)
        if len(content) > max_bytes:
            raise CoworkWebError(f"网页响应超过 {max_bytes} bytes 上限")
    return bytes(content)


async def fetch_url(
    raw_url: str,
    *,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    authorize_target: NetworkAuthorizer | None = None,
) -> WebSnapshot:
    current_url = _normalized_url(raw_url)
    owned_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(settings.cowork_web_timeout_s),
        follow_redirects=False,
        trust_env=False,
        headers={"user-agent": "WorkPilot-Cowork/1.0"},
    )
    try:
        for redirect_count in range(settings.cowork_web_max_redirects + 1):
            # 授权跟随真正将要访问的 origin，而不是最初 URL。跨域重定向必须再次命中
            # network.fetch scope，避免可信站点被用作任意外传跳板。
            if authorize_target is not None:
                await authorize_target(current_url)
            addresses = await _assert_public_target(current_url)
            request_url, host_header, server_name = _pinned_request(current_url, addresses[0])
            try:
                async with active_client.stream(
                    "GET",
                    request_url,
                    headers={"connection": "close", "host": host_header},
                    extensions={"sni_hostname": server_name},
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise CoworkWebError("网页重定向缺少 Location")
                        if redirect_count >= settings.cowork_web_max_redirects:
                            raise CoworkWebError("网页重定向次数超过上限")
                        current_url = _normalized_url(urljoin(current_url, location))
                        continue
                    if response.status_code < 200 or response.status_code >= 300:
                        raise CoworkWebError(f"网页返回 HTTP {response.status_code}")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0]
                    content_type = content_type.strip().casefold()
                    body = await _bounded_response_body(response, settings.cowork_web_max_bytes)
                    status_code = response.status_code
            except httpx.TimeoutException as error:
                raise CoworkWebError("网页读取超时") from error
            except httpx.HTTPError as error:
                raise CoworkWebError("网页连接失败") from error
            if content_type == "application/pdf" or urlsplit(current_url).path.casefold().endswith(
                ".pdf"
            ):
                pdf = await _parse_remote_pdf(body, settings=settings)
                return WebSnapshot(
                    url=raw_url,
                    final_url=current_url,
                    title=pdf.title,
                    content_type="application/pdf",
                    content=pdf.content,
                    truncated=pdf.truncated,
                    status_code=status_code,
                    pdf=pdf,
                )
            if content_type not in {
                "text/html",
                "application/xhtml+xml",
                "text/plain",
                "text/markdown",
            }:
                raise CoworkWebError(f"不支持的网页 Content-Type: {content_type or '未知'}")
            try:
                decoded = body.decode(response.encoding or "utf-8", errors="replace")
            except LookupError:
                decoded = body.decode("utf-8", errors="replace")
            if content_type in {"text/html", "application/xhtml+xml"}:
                parser = _ReadableHtmlParser()
                parser.feed(decoded)
                title, content, links = parser.result()
            else:
                title, content = "", decoded
                links = ()
            limit = settings.cowork_web_text_max_chars
            return WebSnapshot(
                url=raw_url,
                final_url=current_url,
                title=title or urlsplit(current_url).hostname or current_url,
                content_type=content_type,
                content=content[:limit],
                truncated=len(content) > limit,
                status_code=status_code,
                links=links,
            )
        raise CoworkWebError("网页重定向次数超过上限")  # pragma: no cover
    finally:
        if owned_client:
            await active_client.aclose()


async def search_web(
    query: str,
    *,
    max_results: int,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    authorize_target: NetworkAuthorizer | None = None,
) -> list[WebSearchResult]:
    normalized = " ".join(query.split())
    if not normalized:
        raise CoworkWebError("搜索关键词不能为空")
    if not 1 <= max_results <= 20:
        raise CoworkWebError("网页搜索结果数必须位于 1 到 20")
    page = await fetch_url(
        f"https://html.duckduckgo.com/html/?q={quote_plus(normalized)}",
        settings=settings,
        client=client,
        authorize_target=authorize_target,
    )
    results: list[WebSearchResult] = []
    seen: set[str] = set()
    for link in page.links:
        classes = frozenset(link.get("class", "").split())
        if not classes.intersection({"result__a", "result-link"}):
            continue
        raw_url = urljoin(page.final_url, link["url"])
        parsed = urlsplit(raw_url)
        if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            if target:
                raw_url = target
        try:
            result_url = normalize_public_url(raw_url)
        except CoworkWebError:
            continue
        if result_url in seen or urlsplit(result_url).hostname == "duckduckgo.com":
            continue
        seen.add(result_url)
        results.append(
            WebSearchResult(
                title=link["title"],
                url=result_url,
                snippet=" ".join(link.get("snippet", "").split()),
            )
        )
        if len(results) >= max_results:
            break
    return results


async def _parse_remote_pdf(content: bytes, *, settings: Settings) -> PdfSnapshot:
    if len(content) > settings.pdf_max_bytes:
        raise CoworkWebError(
            f"PDF 大小 {len(content)} bytes 超过上限 {settings.pdf_max_bytes} bytes"
        )
    with tempfile.NamedTemporaryFile(
        prefix="workpilot-web-", suffix=".pdf", delete=False
    ) as stream:
        stream.write(content)
        temporary = Path(stream.name)
    try:
        parsed = await read_pdf_file(temporary, settings=settings)
        return replace(parsed, path=Path("remote.pdf"))
    finally:
        await asyncio.to_thread(temporary.unlink, missing_ok=True)
