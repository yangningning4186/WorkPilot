"""只有本机用户能读的 JSON 文件：配置与凭据的载体。

参照 openworker `coworker/secrets.py:write_private_text` 的落盘顺序：
**先把父目录设成 0700，再写临时文件、收紧权限、`os.replace`**。目录权限兜住了
临时文件那一瞬的默认权限窗口——反过来先写文件再 chmod，中间会有一个可读的密钥文件。

Windows 上 `os.chmod` 只切只读位，0600 是**静默 no-op**，文件会继承 SYSTEM /
Administrators 的宽 ACL。那里改用 `icacls` 断继承并只授权当前用户；失败只记日志不抛，
一次 icacls 抖动不该让用户存不进 API Key。

写是全量重写。这些文件都是几十个条目量级的控制面数据，读一次全进内存、改完整份落盘，
比维护增量更新简单得多，也不会出现"写了一半"的中间态。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_IS_WINDOWS = sys.platform == "win32"


def restrict_to_user(path: Path, *, is_dir: bool) -> None:
    if not _IS_WINDOWS:
        os.chmod(path, 0o700 if is_dir else 0o600)
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    domain = os.environ.get("USERDOMAIN")
    account = f"{domain}\\{user}" if domain else user
    # 目录授权必须可继承——(OI) 给文件、(CI) 给子目录。少了这两个标志，
    # /inheritance:r 会留下一个不可继承的 ACE，之后在里面新建的文件 DACL 是空的，
    # 连自己都打不开。
    grant = f"{account}:(OI)(CI)F" if is_dir else f"{account}:F"
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            capture_output=True,
            check=False,
        )
    except OSError:
        logger.warning("icacls 收紧权限失败，文件可能对本机其他账户可读", path=str(path))


def read_private_json(path: Path) -> dict[str, Any]:
    """读不出来一律当空，让调用方走"还没配置"的分支。

    坏掉的 JSON 不抛：这是控制面配置，抛出去会让整个 API 起不来，而用户此刻最需要的
    恰恰是能进设置页把它改回来。
    """

    expanded = path.expanduser()
    try:
        if expanded.is_symlink() or not expanded.is_file():
            return {}
        loaded = json.loads(expanded.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        logger.warning("本机 JSON 存储无法读取，按空处理", path=str(expanded))
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    expanded = path.expanduser()
    expanded.parent.mkdir(parents=True, exist_ok=True)
    try:
        restrict_to_user(expanded.parent, is_dir=True)
    except OSError:
        logger.warning("收紧目录权限失败", path=str(expanded.parent))
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    descriptor, temporary = tempfile.mkstemp(dir=expanded.parent, prefix=f".{expanded.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(body)
        restrict_to_user(Path(temporary), is_dir=False)
        os.replace(temporary, expanded)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
