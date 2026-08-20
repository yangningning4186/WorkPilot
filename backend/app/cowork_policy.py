"""Cowork Store 与 service 共用的无 I/O 授权策略。"""

from pathlib import Path

from app.cowork_contracts import CapabilityDeniedError, CoworkPermissionError

PATH_CAPABILITIES = frozenset(
    {"filesystem.read", "filesystem.write", "office.word.edit", "office.excel.edit"}
)
GLOBAL_CAPABILITIES = frozenset(
    {
        "knowledge.read",
        "network.read",
        "browser.control",
        "shell.execute",
        "external.action",
    }
)
ALL_CAPABILITIES = PATH_CAPABILITIES | GLOBAL_CAPABILITIES


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
