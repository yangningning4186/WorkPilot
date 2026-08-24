"""Cowork Store 与 service 共用的无 I/O 授权策略。"""

import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

from app.cowork_contracts import CapabilityDeniedError, CoworkPermissionError

LEGACY_PATH_CAPABILITIES = frozenset({"office.word.edit", "office.excel.edit"})
LEGACY_GLOBAL_CAPABILITIES = frozenset(
    {
        # 这些旧的全局能力只用于读取、撤销和迁移已有数据库记录。新的授权入口不再
        # 创建它们；运行时按下方 LEGACY_CAPABILITY_FALLBACKS 单向兼容。
        "network.read",
        "browser.control",
        "shell.execute",
        "external.action",
    }
)
ACTIVE_PATH_CAPABILITIES = frozenset({"filesystem.read", "filesystem.write"})
# PATH_CAPABILITIES / ALL_CAPABILITIES 继续包含历史值，只用于读取和撤销旧数据库记录。
# 新的模型运行与授权入口只使用 ACTIVE_CAPABILITIES。
PATH_CAPABILITIES = ACTIVE_PATH_CAPABILITIES | LEGACY_PATH_CAPABILITIES
ACTIVE_GLOBAL_CAPABILITIES = frozenset(
    {
        "knowledge.read",
        "network.fetch",
        "browser.read",
        "browser.write",
        "browser.destructive",
        "sandbox.execute",
        "host.execute",
        "external.read",
        "external.write",
        "external.destructive",
    }
)
GLOBAL_CAPABILITIES = ACTIVE_GLOBAL_CAPABILITIES | LEGACY_GLOBAL_CAPABILITIES
SCOPED_CAPABILITIES = frozenset({"network.fetch"})
LEGACY_CAPABILITIES = LEGACY_PATH_CAPABILITIES | LEGACY_GLOBAL_CAPABILITIES
ALL_CAPABILITIES = PATH_CAPABILITIES | GLOBAL_CAPABILITIES
ACTIVE_CAPABILITIES = ACTIVE_PATH_CAPABILITIES | ACTIVE_GLOBAL_CAPABILITIES

# 兼容只允许旧授权满足新的、更细权限，不允许新权限反向扩大成旧的宽权限。旧网络授权
# 没有 origin 信息，因此仅作为迁移期的显式兼容；API 已经不能再新建它。
LEGACY_CAPABILITY_FALLBACKS: dict[str, tuple[str, ...]] = {
    "network.fetch": ("network.read",),
    "browser.read": ("browser.control",),
    "browser.write": ("browser.control",),
    "browser.destructive": ("browser.control",),
    "host.execute": ("shell.execute",),
    "external.read": ("external.action",),
    "external.write": ("external.action",),
    "external.destructive": ("external.action",),
}


def normalize_network_origin(url: str) -> str:
    """把 http(s) URL 规范化成稳定的 origin scope。"""

    if "\x00" in url:
        raise CoworkPermissionError("网络目标包含非法空字符")
    parsed = urlsplit(url.strip())
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise CoworkPermissionError("网络权限目标必须是绝对 http/https URL")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (UnicodeError, ValueError) as error:
        raise CoworkPermissionError("网络权限目标包含非法主机名或端口") from error
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    rendered_host = f"[{host}]" if ":" in host else host
    authority = rendered_host if port in {None, default_port} else f"{rendered_host}:{port}"
    return f"origin:{parsed.scheme.lower()}://{authority}"


def normalize_network_scope(scope: str) -> str:
    """校验并规范化用户授予的 origin/domain scope。"""

    value = scope.strip()
    if value.startswith("origin:"):
        return normalize_network_origin(value.removeprefix("origin:"))
    if value.startswith("domain:"):
        raw_host = value.removeprefix("domain:").strip().lower().rstrip(".")
        if not raw_host or "/" in raw_host or ":" in raw_host or "@" in raw_host:
            raise CoworkPermissionError("domain scope 只能包含域名")
        try:
            host = raw_host.encode("idna").decode("ascii")
        except UnicodeError as error:
            raise CoworkPermissionError("domain scope 包含非法域名") from error
        labels = host.split(".")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise CoworkPermissionError("domain scope 不能使用 IP 地址")
        if len(labels) < 2 or any(
            not label or label.startswith("-") or label.endswith("-") for label in labels
        ):
            raise CoworkPermissionError("domain scope 包含非法域名")
        return f"domain:{host}"
    # API 也接受完整 URL，避免客户端自行拼接权限协议。
    return normalize_network_origin(value)


def network_scope_allows(scope: str, target_url: str) -> bool:
    normalized_scope = normalize_network_scope(scope)
    origin = normalize_network_origin(target_url)
    if normalized_scope.startswith("origin:"):
        return normalized_scope == origin
    target_host = urlsplit(origin.removeprefix("origin:")).hostname or ""
    allowed_host = normalized_scope.removeprefix("domain:")
    return target_host == allowed_host or target_host.endswith(f".{allowed_host}")


def canonicalize_root(requested_path: str) -> Path:
    if "\x00" in requested_path:
        raise CoworkPermissionError("目录路径包含非法空字符")
    path = Path(requested_path).expanduser()
    if not path.is_absolute():
        raise CoworkPermissionError("会话目录必须使用绝对路径")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise CoworkPermissionError("会话目录不存在或不可访问") from error
    if not resolved.is_dir():
        raise CoworkPermissionError("会话目录必须是已存在的文件夹")
    return resolved


def resolve_target_within_root(root: Path, target: Path) -> Path:
    """解析现有或待创建目标，并拒绝 `..` 与符号链接造成的越界。"""

    canonical_root = root.resolve(strict=True)
    candidate = target if target.is_absolute() else canonical_root / target
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(canonical_root)
    except (OSError, ValueError) as error:
        raise CapabilityDeniedError("目标路径不在已授权会话目录内") from error
    return resolved
