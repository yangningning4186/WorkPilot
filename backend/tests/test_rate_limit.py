import asyncio
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.dependencies import get_ip_rate_limiter
from app.core.config import Settings, get_settings
from app.main import create_app
from app.platform.demo_sessions import consume_question_quota, resolve_demo_session
from app.platform.rate_limit import RateLimitDecision, RedisIpRateLimiter


class RejectingLimiter:
    async def consume(
        self,
        ip: str,
        *,
        rate_per_minute: int,
        burst: int,
    ) -> RateLimitDecision:
        assert ip
        assert rate_per_minute == 20
        assert burst == 5
        return RateLimitDecision(allowed=False, retry_after_s=3, remaining=0)


class BrokenLimiter:
    async def consume(
        self,
        ip: str,
        *,
        rate_per_minute: int,
        burst: int,
    ) -> RateLimitDecision:
        del ip, rate_per_minute, burst
        raise RedisError("down")


def _limited_app(limiter: object):
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        app_env="test", ip_rate_limit_enabled=True
    )
    app.dependency_overrides[get_ip_rate_limiter] = lambda: limiter
    return app


async def test_api_rate_limit_returns_429_with_retry_after_but_health_stays_public() -> None:
    app = _limited_app(RejectingLimiter())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        limited = await client.post("/api/v1/auth/admin/login", json={"password": "irrelevant"})
        health = await client.get("/health/live")

    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "3"
    assert health.status_code == 200


async def test_rate_limit_fails_closed_when_redis_is_unavailable() -> None:
    app = _limited_app(BrokenLimiter())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/auth/admin/login", json={"password": "irrelevant"})
    assert response.status_code == 503


@pytest.mark.integration
async def test_redis_token_bucket_enforces_burst_atomically() -> None:
    prefix = f"workpilot:test-rate:{uuid4()}"
    client = Redis.from_url(Settings().redis_url, decode_responses=True)
    limiter = RedisIpRateLimiter(client, prefix=prefix)
    try:
        decisions = [
            await limiter.consume("203.0.113.10", rate_per_minute=1, burst=5) for _ in range(6)
        ]
        assert [decision.allowed for decision in decisions] == [True] * 5 + [False]
        assert decisions[-1].retry_after_s > 0
    finally:
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


@pytest.mark.integration
async def test_session_question_quota_does_not_overshoot_under_concurrency(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
) -> None:
    resolved = await resolve_demo_session(db_session, cookie_token=None, ttl_s=3600)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def consume_once() -> bool:
        async with factory() as session:
            consumed = await consume_question_quota(
                session,
                demo_session_id=resolved.session.id,
                limit=3,
            )
            await session.commit()
            return consumed

    results = await asyncio.gather(*(consume_once() for _ in range(10)))

    assert sum(results) == 3
    assert (
        await db_session.execute(
            text("SELECT question_count FROM demo_sessions WHERE id = :id"),
            {"id": resolved.session.id},
        )
    ).scalar_one() == 3
