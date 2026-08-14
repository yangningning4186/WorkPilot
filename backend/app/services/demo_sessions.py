"""匿名 demo session：浏览器持有原始 token，数据库只保存 SHA-256。"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7


@dataclass(frozen=True)
class DemoSession:
    id: UUID
    expires_at: datetime


@dataclass(frozen=True)
class ResolvedDemoSession:
    session: DemoSession
    cookie_token: str | None


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def resolve_demo_session(
    session: AsyncSession,
    *,
    cookie_token: str | None,
    ttl_s: int,
) -> ResolvedDemoSession:
    """解析有效 cookie；缺失、伪造、撤销或过期时签发全新 session。"""

    if cookie_token and len(cookie_token) <= 256:
        row = (
            (
                await session.execute(
                    text(
                        """
                        UPDATE demo_sessions
                        SET last_seen_at = now()
                        WHERE token_hash = :token_hash
                          AND revoked_at IS NULL
                          AND expires_at > now()
                        RETURNING id, expires_at
                        """
                    ),
                    {"token_hash": hash_session_token(cookie_token)},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            await session.commit()
            return ResolvedDemoSession(session=DemoSession(**row), cookie_token=None)

    raw_token = secrets.token_urlsafe(32)
    row = (
        (
            await session.execute(
                text(
                    """
                    INSERT INTO demo_sessions (id, token_hash, expires_at)
                    VALUES (:id, :token_hash, now() + make_interval(secs => :ttl_s))
                    RETURNING id, expires_at
                    """
                ),
                {
                    "id": uuid7(),
                    "token_hash": hash_session_token(raw_token),
                    "ttl_s": ttl_s,
                },
            )
        )
        .mappings()
        .one()
    )
    await session.commit()
    return ResolvedDemoSession(session=DemoSession(**row), cookie_token=raw_token)
