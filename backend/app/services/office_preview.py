"""Office 交付物的真实版面预览。

桌面端优先复用 macOS Quick Look，其他部署优先使用 LibreOffice 转 PDF。渲染结果只写入
WorkPilot 自有缓存，不在用户工作区旁落临时文件；所有子进程均使用 argv、最小环境与超时。
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

_SCRIPT = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_META_REFRESH = re.compile(
    r"<meta\b[^>]*http-equiv\s*=\s*['\"]?refresh['\"]?[^>]*>",
    re.IGNORECASE,
)
_ACTIVE_TAG = re.compile(
    r"<(?:base|object|embed|iframe)\b[^>]*>.*?</(?:object|iframe)\s*>|<(?:base|object|embed|iframe)\b[^>]*?/?>",
    re.IGNORECASE | re.DOTALL,
)
_EVENT_HANDLER = re.compile(r"\s+on[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_LOCAL_SOURCE = re.compile(
    r"(?P<prefix>\b(?:src|poster)\s*=\s*)(?P<quote>['\"])(?P<value>[^'\"]+)(?P=quote)",
    re.IGNORECASE,
)
_CSS_URL = re.compile(r"url\((?P<quote>['\"]?)(?P<value>[^)'\"]+)(?P=quote)\)", re.IGNORECASE)
_ALLOWED_SUFFIXES = frozenset({".docx", ".xlsx", ".pptx"})


class OfficePreviewError(RuntimeError):
    """渲染器存在但无法安全完成预览。"""


@dataclass(frozen=True)
class OfficePreview:
    path: Path
    media_type: str
    mode: str


def render_office_preview(
    source: Path,
    *,
    cache_root: Path,
    timeout_s: float,
    max_source_bytes: int,
    max_cache_entries: int,
) -> OfficePreview | None:
    """返回缓存后的真实渲染结果；没有可用渲染器时返回 ``None``。"""

    suffix = source.suffix.casefold()
    if suffix not in _ALLOWED_SUFFIXES:
        raise ValueError("Office 预览仅支持 .docx、.xlsx 与 .pptx")
    size = source.stat().st_size
    if size > max_source_bytes:
        raise OfficePreviewError("Office 文件过大，无法生成在线版面预览")
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_root.chmod(0o700)
    digest = _source_digest(source)
    target = cache_root / digest

    quicklook = Path("/usr/bin/qlmanage")
    if sys.platform == "darwin" and quicklook.is_file():
        cached = target / "preview.html"
        if cached.is_file():
            _touch(cached)
            return OfficePreview(cached, "text/html; charset=utf-8", "quicklook")
        try:
            rendered = _render_quicklook(source, target, quicklook, timeout_s)
        except OfficePreviewError:
            rendered = None
        if rendered is not None:
            _prune(cache_root, keep=digest, max_entries=max_cache_entries)
            return OfficePreview(rendered, "text/html; charset=utf-8", "quicklook")

    soffice = _find_soffice()
    if soffice is None:
        return None
    cached = target / "preview.pdf"
    if cached.is_file():
        _touch(cached)
        return OfficePreview(cached, "application/pdf", "libreoffice")
    rendered = _render_libreoffice(source, target, soffice, timeout_s)
    _prune(cache_root, keep=digest, max_entries=max_cache_entries)
    return OfficePreview(rendered, "application/pdf", "libreoffice")


def _source_digest(source: Path) -> str:
    digest = hashlib.sha256()
    digest.update(b"office-preview.v1\0")
    with source.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _minimal_environment() -> dict[str, str]:
    environment = {
        "HOME": tempfile.gettempdir(),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", os.environ.get("LANG", "C.UTF-8")),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
        "TMPDIR": tempfile.gettempdir(),
    }
    if "SYSTEMROOT" in os.environ:
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return environment


def _find_soffice() -> Path | None:
    configured = os.environ.get("WORKPILOT_SOFFICE_PATH", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
        Path("C:/Program Files/LibreOffice/program/soffice.exe"),
        Path("C:/Program Files (x86)/LibreOffice/program/soffice.exe"),
        Path(shutil.which("soffice") or ""),
        Path(shutil.which("libreoffice") or ""),
    ]
    return next((path for path in candidates if path is not None and path.is_file()), None)


def _run_renderer(argv: list[str], *, timeout_s: float) -> None:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            cwd=tempfile.gettempdir(),
            env=_minimal_environment(),
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OfficePreviewError("Office 版面渲染器启动失败或超时") from error
    if completed.returncode != 0:
        detail = " ".join((completed.stderr or completed.stdout).split())[:300]
        raise OfficePreviewError(f"Office 版面渲染失败：{detail or '渲染器未返回原因'}")


def _render_quicklook(
    source: Path, target: Path, executable: Path, timeout_s: float
) -> Path | None:
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix=".quicklook-", dir=target) as raw_staging:
        staging = Path(raw_staging)
        _run_renderer(
            [str(executable), "-p", "-o", str(staging), str(source)],
            timeout_s=timeout_s,
        )
        previews = sorted(staging.glob("*.qlpreview/Preview.html"))
        if not previews:
            return None
        bundle = previews[0].parent.resolve(strict=True)
        raw = previews[0].read_text(encoding="utf-8", errors="replace")
        sanitized = _sanitize_quicklook_html(raw, bundle)
        output = target / "preview.html"
        temporary = target / ".preview.html.tmp"
        temporary.write_text(sanitized, encoding="utf-8")
        os.replace(temporary, output)
        output.chmod(0o600)
        return output


def _sanitize_quicklook_html(raw: str, bundle: Path) -> str:
    value = _SCRIPT.sub("", raw)
    value = _META_REFRESH.sub("", value)
    value = _ACTIVE_TAG.sub("", value)
    value = _EVENT_HANDLER.sub("", value)
    value = re.sub(r"javascript\s*:", "", value, flags=re.IGNORECASE)

    def embed(match: re.Match[str]) -> str:
        uri = _resource_data_uri(bundle, match.group("value"))
        return f'{match.group("prefix")}"{uri}"' if uri is not None else ""

    def embed_css(match: re.Match[str]) -> str:
        uri = _resource_data_uri(bundle, match.group("value"))
        return f'url("{uri}")' if uri is not None else "url()"

    value = _LOCAL_SOURCE.sub(embed, value)
    value = _CSS_URL.sub(embed_css, value)
    return value


def _resource_data_uri(bundle: Path, raw: str) -> str | None:
    if raw.startswith("data:"):
        return raw
    if "://" in raw or raw.startswith(("/", "#")):
        return None
    try:
        candidate = (bundle / raw).resolve(strict=True)
        candidate.relative_to(bundle)
    except (OSError, ValueError):
        return None
    if not candidate.is_file() or candidate.stat().st_size > 8 * 1024 * 1024:
        return None
    media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(candidate.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _render_libreoffice(source: Path, target: Path, executable: Path, timeout_s: float) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix=".libreoffice-", dir=target) as raw_staging:
        staging = Path(raw_staging)
        profile = staging / "profile"
        profile.mkdir()
        _run_renderer(
            [
                str(executable),
                "--headless",
                "--nologo",
                "--nodefault",
                "--nolockcheck",
                "--norestore",
                f"-env:UserInstallation={profile.as_uri()}",
                "--convert-to",
                "pdf",
                "--outdir",
                str(staging),
                str(source),
            ],
            timeout_s=timeout_s,
        )
        generated = staging / f"{source.stem}.pdf"
        if not generated.is_file() or generated.stat().st_size == 0:
            raise OfficePreviewError("LibreOffice 没有生成 PDF 预览")
        output = target / "preview.pdf"
        os.replace(generated, output)
        output.chmod(0o600)
        return output


def _touch(path: Path) -> None:
    try:
        path.touch(exist_ok=True)
    except OSError:
        pass


def _prune(cache_root: Path, *, keep: str, max_entries: int) -> None:
    entries = sorted(
        (item for item in cache_root.iterdir() if item.is_dir() and item.name != keep),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    )
    for stale in entries[max(0, max_entries - 1) :]:
        shutil.rmtree(stale, ignore_errors=True)
