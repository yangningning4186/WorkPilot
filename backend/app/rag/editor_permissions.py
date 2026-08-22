"""绑定在 owner 会话令牌上的限时写授权。

进程内实现，不落盘。这个授权不可能活得比它依附的 owner 会话更久，而 owner 会话本身
（`app/platform/admin_sessions.py`）已经是进程内的——重启后要重新输口令，那时这条
授权也该跟着没有。持久化它反而会造出"会话没了、授权还在"的组合。

openworker 的 session `grants` 走的是另一条路（持久化进会话记录），但它们依附的是
本来就跨进程存活的会话记录，前提不同。
"""

import hashlib
import threading
from time import monotonic
from typing import Protocol


class EditorPermissionStore(Protocol):
    async def grant(self, token: str, *, ttl_s: int) -> None: ...

    async def ttl(self, token: str) -> int: ...

    async def revoke(self, token: str) -> None: ...


class InProcessEditorPermissionStore:
    """token 的 sha256 → 到期时刻。

    仍然只存摘要，不存令牌原文：这份授权表会出现在异常快照和内存 dump 里，
    存原文等于给了一条不用登录就能复用的路径。
    """

    def __init__(self) -> None:
        self._expiry: dict[str, float] = {}
        self._lock = threading.Lock()

    def _key(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _sweep(self, now: float) -> None:
        for key, expires_at in list(self._expiry.items()):
            if expires_at <= now:
                del self._expiry[key]

    async def grant(self, token: str, *, ttl_s: int) -> None:
        if not token or len(token) > 256:
            raise ValueError("owner session token 无效")
        now = monotonic()
        with self._lock:
            self._sweep(now)
            self._expiry[self._key(token)] = now + ttl_s

    async def ttl(self, token: str) -> int:
        """剩余秒数；-2 表示没有授权，与原 Redis TTL 的语义一致。"""

        if not token or len(token) > 256:
            return -2
        now = monotonic()
        with self._lock:
            expires_at = self._expiry.get(self._key(token))
            if expires_at is None:
                return -2
            if expires_at <= now:
                del self._expiry[self._key(token)]
                return -2
        return int(expires_at - now)

    async def revoke(self, token: str) -> None:
        if token and len(token) <= 256:
            with self._lock:
                self._expiry.pop(self._key(token), None)
