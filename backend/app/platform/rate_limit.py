"""Redis 原子 token bucket：请求层按 IP 限流。"""

import hashlib
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol, cast

from redis.asyncio import Redis

_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local rate_per_ms = tonumber(ARGV[1]) / 60000
local capacity = tonumber(ARGV[2])
local now = redis.call('TIME')
local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
local bucket = redis.call('HMGET', key, 'tokens', 'updated_ms')
local tokens = tonumber(bucket[1]) or capacity
local updated_ms = tonumber(bucket[2]) or now_ms
local elapsed_ms = math.max(0, now_ms - updated_ms)
tokens = math.min(capacity, tokens + elapsed_ms * rate_per_ms)

local allowed = 0
local retry_after_ms = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry_after_ms = math.ceil((1 - tokens) / rate_per_ms)
end

redis.call('HSET', key, 'tokens', tokens, 'updated_ms', now_ms)
local ttl_ms = math.ceil(capacity / rate_per_ms) + 60000
redis.call('PEXPIRE', key, ttl_ms)
return {allowed, retry_after_ms, math.floor(tokens)}
"""


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_s: int
    remaining: int


class IpRateLimiter(Protocol):
    async def consume(
        self,
        ip: str,
        *,
        rate_per_minute: int,
        burst: int,
    ) -> RateLimitDecision: ...


class RedisIpRateLimiter:
    def __init__(self, client: Redis, *, prefix: str = "workpilot:rate:ip") -> None:
        self._client = client
        self._prefix = prefix

    async def consume(
        self,
        ip: str,
        *,
        rate_per_minute: int,
        burst: int,
    ) -> RateLimitDecision:
        digest = hashlib.sha256(ip.encode("utf-8")).hexdigest()
        result = await cast(
            Awaitable[list[int | str | bytes]],
            self._client.eval(
                _TOKEN_BUCKET_LUA,
                1,
                f"{self._prefix}:{digest}",
                str(rate_per_minute),
                str(burst),
            ),
        )
        allowed, retry_after_ms, remaining = (int(value) for value in result)
        retry_after_s = max(1, (retry_after_ms + 999) // 1000) if not allowed else 0
        return RateLimitDecision(
            allowed=bool(allowed),
            retry_after_s=retry_after_s,
            remaining=max(0, remaining),
        )
