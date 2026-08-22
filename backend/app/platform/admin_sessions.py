"""owner 登录：口令校验与进程内会话。

**为什么不再用 Redis。** 这是本机单用户应用，只有一个进程在服务。会话跨进程共享没有
用户，跨重启存活也没有价值——重启后重新输一次口令，比为此维护一个外部服务便宜得多。
桌面壳根本走不到这里：它带的是启动令牌（`DesktopLaunchTokenMiddleware`），这条路径
只服务浏览器里打开的开发界面。

到期靠惰性清理而不是定时器：会话数量是个位数，遍历一次比养一个后台任务简单。
"""

from __future__ import annotations

import secrets
import time

import bcrypt


class AdminSessionStore:
    """进程内会话表。token → 到期时间戳。"""

    def __init__(self, *, ttl_s: int) -> None:
        self._ttl_s = ttl_s
        self._sessions: dict[str, float] = {}

    def _sweep(self) -> None:
        now = time.time()
        for token, expires_at in list(self._sessions.items()):
            if expires_at <= now:
                del self._sessions[token]

    async def issue(self) -> str:
        self._sweep()
        token = secrets.token_urlsafe(32)
        self._sessions[token] = time.time() + self._ttl_s
        return token

    async def validate(self, token: str) -> bool:
        self._sweep()
        return token in self._sessions

    async def revoke(self, token: str) -> None:
        self._sessions.pop(token, None)


def verify_admin_password(password: str, password_hash: str) -> bool:
    """恒定时间口令校验。

    没有配置 hash 时返回 False 而不是放行——把"没设密码"当成"任何密码都对"，
    是把一台开发机变成一个公开后门最常见的方式。
    """
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def hash_admin_password(password: str) -> str:
    """生成 bcrypt hash，给 `uv run python -m app.cli.hash_admin_password` 用。"""
    if not password:
        raise ValueError("admin 密码不能为空")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


__all__ = ["AdminSessionStore", "hash_admin_password", "verify_admin_password"]
