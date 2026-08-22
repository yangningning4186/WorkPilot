"""Redis 最后两个消费者换成进程内实现之后的行为约束。"""

from time import monotonic
from unittest.mock import patch

import pytest

from app.rag.editor_permissions import InProcessEditorPermissionStore
from workpilot_ai.cache import (
    InProcessCompletionCache,
    is_cacheable,
    shared_completion_cache,
)
from workpilot_ai.types import CompletionResult, Usage


def _result(text: str) -> CompletionResult:
    return CompletionResult(
        text=text, model="m", provider="p", usage=Usage(input_tokens=10, output_tokens=5)
    )


async def test_cache_evicts_least_recently_used_and_expires_on_ttl() -> None:
    cache = InProcessCompletionCache(max_entries=2)
    await cache.set("a", _result("A"), ttl_s=60)
    await cache.set("b", _result("B"), ttl_s=60)
    assert await cache.get("a") is not None  # a 变成最近使用

    await cache.set("c", _result("C"), ttl_s=60)
    # 封顶淘汰的是 b 而不是 a；不封顶就是一条随运行时长单调增长的内存曲线。
    assert await cache.get("b") is None
    assert (await cache.get("a")).text == "A"  # type: ignore[union-attr]

    with patch("workpilot_ai.cache.monotonic", return_value=monotonic() + 3_600):
        assert await cache.get("a") is None


async def test_cache_returns_the_usage_it_stored() -> None:
    cache = InProcessCompletionCache()
    await cache.set("k", _result("A"), ttl_s=60)
    hit = await cache.get("k")

    # 命中要照原样带回 usage：网关靠它记审计，丢了就算不出命中率。
    assert hit is not None
    assert hit.usage.input_tokens == 10
    assert hit.usage.output_tokens == 5


def test_shared_cache_is_one_instance_per_process() -> None:
    # 每建一个网关就新建一个缓存等于恒不命中，而调用方无从察觉。
    assert shared_completion_cache() is shared_completion_cache(max_entries=9)


@pytest.mark.parametrize(
    ("temperature", "mode", "expected"),
    [(0.0, "online", True), (0.7, "online", False), (0.0, "evaluation", False)],
)
def test_cacheable_rules_survive_the_backend_swap(
    temperature: float, mode: str, expected: bool
) -> None:
    # 采样不缓存（否则等于偷偷变成贪心解码），评测不缓存（命中的那条根本没过模型）。
    assert is_cacheable(temperature=temperature, mode=mode) is expected


async def test_editor_permission_expires_and_never_stores_the_raw_token() -> None:
    store = InProcessEditorPermissionStore()
    token = "owner-session-token"

    assert await store.ttl(token) == -2  # 没授权，与原 Redis TTL 语义一致
    await store.grant(token, ttl_s=120)
    assert 0 < await store.ttl(token) <= 120
    assert all(token not in key for key in store._expiry)

    with patch("app.rag.editor_permissions.monotonic", return_value=monotonic() + 600):
        assert await store.ttl(token) == -2
    assert store._expiry == {}

    await store.grant(token, ttl_s=120)
    await store.revoke(token)
    assert await store.ttl(token) == -2
    with pytest.raises(ValueError, match="无效"):
        await store.grant("x" * 300, ttl_s=120)
