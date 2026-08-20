"""demo admin 的密码校验与 Redis 会话。"""

import hashlib
import secrets
from typing import Protocol

import bcrypt
from redis.asyncio import Redis


class AdminSessionStore(Protocol):
    async def issue(self, *, ttl_s: int) -> str: ...

    async def validate(self, token: str) -> bool: ...

    async def revoke(self, token: str) -> None: ...


class RedisAdminSessionStore:
    def __init__(self, client: Redis, *, prefix: str = "workpilot:admin-session") -> None:
        self._client = client
        self._prefix = prefix

    def _key(self, token: str) -> str:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return f"{self._prefix}:{digest}"

    async def issue(self, *, ttl_s: int) -> str:
        token = secrets.token_urlsafe(32)
        await self._client.set(self._key(token), "1", ex=ttl_s)
        return token

    async def validate(self, token: str) -> bool:
        if not token or len(token) > 256:
            return False
        return bool(await self._client.exists(self._key(token)))

    async def revoke(self, token: str) -> None:
        if token and len(token) <= 256:
            await self._client.delete(self._key(token))


def verify_admin_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def hash_admin_password(password: str) -> str:
    if not password:
        raise ValueError("admin 密码不能为空")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
