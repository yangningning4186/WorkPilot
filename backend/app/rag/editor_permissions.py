"""Time-limited write grants bound to the current owner session token."""

import hashlib
from typing import Protocol

from redis.asyncio import Redis


class EditorPermissionStore(Protocol):
    async def grant(self, token: str, *, ttl_s: int) -> None: ...

    async def ttl(self, token: str) -> int: ...

    async def revoke(self, token: str) -> None: ...


class RedisEditorPermissionStore:
    def __init__(self, client: Redis, *, prefix: str = "workpilot:editor-permission") -> None:
        self._client = client
        self._prefix = prefix

    def _key(self, token: str) -> str:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return f"{self._prefix}:{digest}"

    async def grant(self, token: str, *, ttl_s: int) -> None:
        if not token or len(token) > 256:
            raise ValueError("owner session token 无效")
        await self._client.set(self._key(token), "local_office_write", ex=ttl_s)

    async def ttl(self, token: str) -> int:
        if not token or len(token) > 256:
            return -2
        return int(await self._client.ttl(self._key(token)))

    async def revoke(self, token: str) -> None:
        if token and len(token) <= 256:
            await self._client.delete(self._key(token))
