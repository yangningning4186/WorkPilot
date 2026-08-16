"""精确缓存与 prompt 缓存前缀（docs/07 §6）。

精确缓存的价值不在命中率，在于命中时成本是零。所以这组用例几乎全在测**什么时候
不该命中**——一次错误命中会安静地返回"看起来对"的答案，比不命中贵得多。

最后一组测 prompt 缓存的前提：系统提示必须在最前面且逐字稳定。§6 点名这是
"很常见的低级错误"，而它不会报错，只会让 provider 侧前缀缓存命中率悄悄归零。
"""

from decimal import Decimal

import pytest

from app.llm.cache import CompletionCache, completion_cache_key, is_cacheable
from app.llm.gateway import ModelGateway
from app.llm.routing import parse_routing_table
from app.llm.types import CompletionResult, Message, Usage
from app.services.evidence_sufficiency import SYSTEM_PROMPT as GATE_PROMPT
from tests.fakes import DeterministicProvider
from tests.test_model_routing import ENV, RecordingSink, _minimal, _pool


class MemoryCache(CompletionCache):
    def __init__(self) -> None:
        self.store: dict[str, CompletionResult] = {}
        self.gets = 0
        self.sets = 0

    async def get(self, key: str) -> CompletionResult | None:
        self.gets += 1
        return self.store.get(key)

    async def set(self, key: str, result: CompletionResult, *, ttl_s: int) -> None:
        self.sets += 1
        self.store[key] = result


class CountingProvider(DeterministicProvider):
    def __init__(self, text: str) -> None:
        super().__init__(4, completion_text=text)
        self.completions = 0

    async def complete(self, messages, *, max_tokens, temperature):  # type: ignore[no-untyped-def]
        self.completions += 1
        return await super().complete(messages, max_tokens=max_tokens, temperature=temperature)


def _gateway(provider: DeterministicProvider, cache: CompletionCache | None, **kwargs):
    return ModelGateway(provider, embedding_dimensions=4, completion_cache=cache, **kwargs)


# ------------------------------------------------------------------ 键的构成


def test_key_changes_with_every_input_that_changes_the_output() -> None:
    base = {
        "tier": "main",
        "model": "m",
        "provider": "p",
        "messages": [Message(role="user", content="问题")],
        "max_tokens": 100,
        "temperature": 0.0,
    }
    key = completion_cache_key(**base)  # type: ignore[arg-type]

    for field, value in (
        ("tier", "heavy"),
        ("model", "other"),
        ("provider", "other"),
        ("max_tokens", 200),
        ("messages", [Message(role="user", content="另一个问题")]),
        # enable_thinking 一开一关输出完全不同，而 messages/model 都没变。
        ("request_fingerprint", "thinking=True"),
    ):
        assert completion_cache_key(**{**base, field: value}) != key, f"{field} 变了键却没变"


def test_role_is_part_of_the_key() -> None:
    """同样一段文字放 system 还是 user，模型行为完全不同。"""

    as_user = completion_cache_key(
        tier="main",
        model="m",
        provider="p",
        messages=[Message(role="user", content="X")],
        max_tokens=10,
        temperature=0.0,
    )
    as_system = completion_cache_key(
        tier="main",
        model="m",
        provider="p",
        messages=[Message(role="system", content="X")],
        max_tokens=10,
        temperature=0.0,
    )
    assert as_user != as_system


def test_sampling_and_evaluation_are_never_cacheable() -> None:
    # 缓存采样输出等于把 temperature 偷偷改成 0，调用方无从察觉。
    assert is_cacheable(temperature=0.7, mode="online") is False
    # 命中意味着这一条根本没过模型，却会被算进跑批指标里。
    assert is_cacheable(temperature=0.0, mode="evaluation") is False
    assert is_cacheable(temperature=0.0, mode="online") is True


# ------------------------------------------------------------------ 网关行为


async def test_second_identical_call_hits_and_skips_the_model() -> None:
    provider = CountingProvider("答案")
    cache = MemoryCache()
    sink = RecordingSink()
    gateway = _gateway(provider, cache, audit_sink=sink)
    messages = [Message(role="user", content="同一个问题")]

    first = await gateway.complete(messages, task_type="generate")
    second = await gateway.complete(messages, task_type="generate")

    assert first.text == second.text == "答案"
    assert provider.completions == 1, "第二次不该再打模型"
    assert [record.cached for record in sink.records] == [False, True]


async def test_cache_hit_costs_nothing_but_still_lands_in_the_audit_trail() -> None:
    """命中要记账：不记的话看板算不出命中率，这一层的价值就没法证明（§9）。"""

    provider = CountingProvider("答案")
    cache = MemoryCache()
    sink = RecordingSink()
    gateway = _gateway(provider, cache, audit_sink=sink)
    messages = [Message(role="user", content="问题")]

    await gateway.complete(messages, task_type="generate")
    await gateway.complete(messages, task_type="generate")

    hit = sink.records[1]
    assert hit.cached is True
    assert hit.cache_type == "exact"
    assert hit.cost_usd == Decimal(0)
    assert hit.success is True


async def test_sampled_calls_are_neither_read_nor_written() -> None:
    provider = CountingProvider("答案")
    cache = MemoryCache()
    gateway = _gateway(provider, cache)
    messages = [Message(role="user", content="问题")]

    await gateway.complete(messages, task_type="generate", temperature=0.7)
    await gateway.complete(messages, task_type="generate", temperature=0.7)

    assert provider.completions == 2
    assert cache.gets == 0 and cache.sets == 0


async def test_evaluation_mode_never_touches_the_cache() -> None:
    provider = CountingProvider("答案")
    cache = MemoryCache()
    gateway = _gateway(provider, cache, mode="evaluation")
    messages = [Message(role="user", content="问题")]

    await gateway.complete(messages, task_type="generate")
    await gateway.complete(messages, task_type="generate")

    assert provider.completions == 2
    assert cache.gets == 0 and cache.sets == 0


async def test_different_tiers_do_not_share_cache_entries() -> None:
    """帕累托实验切八种配置时，共享缓存会让八条曲线测出来一模一样。"""

    light = CountingProvider("light 的答案")
    heavy = CountingProvider("heavy 的答案")
    cache = MemoryCache()
    table = parse_routing_table(_minimal(), ENV)
    gateway = ModelGateway(
        light,
        embedding_dimensions=4,
        completion_cache=cache,
        pool=_pool(table, {"light": light, "main": light, "heavy": heavy}),
    )
    messages = [Message(role="user", content="同一个问题")]

    first = await gateway.complete(messages, task_type="generate", tier_override="main")
    second = await gateway.complete(messages, task_type="generate", tier_override="heavy")

    assert first.text == "light 的答案"
    assert second.text == "heavy 的答案"


async def test_failures_are_not_cached() -> None:
    """一次抖动被钉住 24 小时，比不缓存糟得多。"""

    class Flaky(DeterministicProvider):
        def __init__(self) -> None:
            super().__init__(4, completion_text="好了")
            self.calls = 0

        async def complete(self, messages, *, max_tokens, temperature):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("抖了一下")
            return await super().complete(
                messages, max_tokens=max_tokens, temperature=temperature
            )

    provider = Flaky()
    cache = MemoryCache()
    gateway = _gateway(provider, cache)
    messages = [Message(role="user", content="问题")]

    with pytest.raises(RuntimeError):
        await gateway.complete(messages, task_type="generate")
    assert cache.sets == 0

    result = await gateway.complete(messages, task_type="generate")
    assert result.text == "好了"


async def test_streaming_is_not_cached_by_design() -> None:
    """流式刻意不接缓存。

    它要和取消、预算结算、"吐字之后不许 fallback"三件事同时正确，而流式又是
    唯一的用户可见路径。这一版先不动它，等非流式那层的命中率数据出来再说。
    """

    provider = DeterministicProvider(4, completion_text="流式答案")
    cache = MemoryCache()
    gateway = _gateway(provider, cache)
    messages = [Message(role="user", content="问题")]

    async for _ in gateway.stream(messages, task_type="generate"):
        pass

    assert cache.gets == 0 and cache.sets == 0


async def test_a_broken_cache_never_blocks_the_call() -> None:
    class BrokenCache(CompletionCache):
        async def get(self, key: str) -> CompletionResult | None:
            raise AssertionError("不该被调用")  # 由 RedisCompletionCache 自己吞掉

        async def set(self, key: str, result: CompletionResult, *, ttl_s: int) -> None:
            raise AssertionError("不该被调用")

    # 这里验的是 Redis 实现的降级：RedisError 被吞成"未命中"。
    from redis.exceptions import RedisError

    from app.llm.cache import RedisCompletionCache

    class ExplodingRedis:
        async def get(self, key: str) -> str:
            raise RedisError("连不上")

        async def set(self, key: str, value: str, *, ex: int) -> None:
            raise RedisError("连不上")

    redis_cache = RedisCompletionCache(ExplodingRedis())  # type: ignore[arg-type]
    provider = CountingProvider("答案")
    gateway = _gateway(provider, redis_cache)

    result = await gateway.complete([Message(role="user", content="问题")], task_type="generate")

    assert result.text == "答案"
    assert provider.completions == 1


async def test_corrupt_cache_entry_is_treated_as_a_miss() -> None:
    from app.llm.cache import RedisCompletionCache

    class GarbageRedis:
        async def get(self, key: str) -> str:
            return "{ 这不是合法 JSON"

        async def set(self, key: str, value: str, *, ex: int) -> None:
            return None

    cache = RedisCompletionCache(GarbageRedis())  # type: ignore[arg-type]
    provider = CountingProvider("真答案")
    gateway = _gateway(provider, cache)

    result = await gateway.complete([Message(role="user", content="问题")], task_type="generate")

    assert result.text == "真答案"


async def test_round_trip_preserves_usage_for_cost_reporting() -> None:
    from app.llm.cache import RedisCompletionCache

    class DictRedis:
        def __init__(self) -> None:
            self.data: dict[str, str] = {}

        async def get(self, key: str) -> str | None:
            return self.data.get(key)

        async def set(self, key: str, value: str, *, ex: int) -> None:
            self.data[key] = value

    cache = RedisCompletionCache(DictRedis())  # type: ignore[arg-type]
    await cache.set(
        "k",
        CompletionResult(
            text="答案", model="m", provider="p", usage=Usage(input_tokens=7, output_tokens=3)
        ),
        ttl_s=60,
    )
    restored = await cache.get("k")

    assert restored is not None
    assert restored.usage == Usage(input_tokens=7, output_tokens=3)
    assert restored.model == "m"


# -------------------------------------------------------------- prompt 缓存


def test_system_prompt_is_a_stable_prefix() -> None:
    """系统提示里不能有随调用变化的内容。

    provider 侧前缀缓存靠"前缀逐字相同"命中。往系统提示里塞当前时间、trace_id、
    用户记忆，会让整个前缀每次都变——缓存命中率归零，而且没有任何报错。
    """

    volatile = ("{now", "{timestamp", "{trace", "{session", "{run_id", "%(", "{}")
    for marker in volatile:
        assert marker not in GATE_PROMPT, f"证据门控系统提示里出现了动态内容标记 {marker}"


async def test_dynamic_content_stays_out_of_the_message_prefix() -> None:
    """两次不同输入构造出的消息，system 段必须逐字相同。"""

    from uuid6 import uuid7

    from app.retrieval.citations import EvidenceSegment
    from app.services.evidence_sufficiency import assess_evidence_sufficiency

    good = '{"sufficient":true,"reason":"够","support_ids":["S1"],"missing_aspects":[]}'

    def evidence(text: str) -> list[EvidenceSegment]:
        return [
            EvidenceSegment(
                citation_id="S1",
                block_id=uuid7(),
                version_id=uuid7(),
                document_id=uuid7(),
                title="标题",
                source_uri="s.md",
                quote=text,
                char_start=0,
                char_end=len(text),
                heading_path=[],
                locations=[],
            )
        ]

    provider = DeterministicProvider(4, completion_texts=[good, good])
    gateway = _gateway(provider, None)
    prefixes: list[str] = []
    for index in range(2):
        await assess_evidence_sufficiency(
            gateway,
            query=f"问题{index}",
            evidence=evidence(f"证据{index}"),
            top_score=0.9,
            second_score=0.5,
            score_margin=0.4,
            low_margin=False,
        )
        prefixes.append(provider.last_messages[0].content)

    assert prefixes[0] == prefixes[1]
    assert provider.last_messages[0].role == "system"
