"""精确缓存（docs/07 §6 第一层）。

命中率注定不高——同一个人不会逐字重复提问。但它的成本是**零**：命中就省下一整次
推理，没命中只多一次 Redis GET。§6 里真正危险的是语义缓存（相似 query 不等于
相同意图，`"A 的优点"` 和 `"A 的缺点"` 向量相似度很高），那一层在 Backlog，这里不做。

四条不缓存的硬规则：

1. **`temperature > 0` 不缓存。** 采样的意义就是每次不同，缓存等于偷偷把它变成
   贪心解码，而调用方无从察觉。
2. **评测模式不缓存。** 命中意味着这一条根本没过模型，跑批却会把它算进指标里。
   这和 §7.4 禁 fallback 是同一个理由：台账必须对应真实发生的调用。
3. **失败不缓存。** 只写成功结果，否则一次抖动会被钉住 24 小时。
4. **键里必须带档位与模型。** 否则帕累托实验（§4）切配置时会拿到上一个配置的答案，
   八种配置测出来一模一样。

语料更新不需要额外处理：证据正文就在 prompt 里，内容变了键就变了。这是精确缓存
相对语义缓存的一个便宜之处——后者才需要 `corpus_version`。
"""

import hashlib
import json
from typing import Protocol

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.llm.types import CompletionResult, Message, Usage

logger = structlog.get_logger(__name__)


class CompletionCache(Protocol):
    async def get(self, key: str) -> CompletionResult | None: ...

    async def set(self, key: str, result: CompletionResult, *, ttl_s: int) -> None: ...


def completion_cache_key(
    *,
    tier: str,
    model: str,
    provider: str,
    messages: list[Message],
    max_tokens: int,
    temperature: float,
    request_fingerprint: str = "",
) -> str:
    """把决定输出的全部输入摊平成一个 sha256。

    少放一个字段就是一次错误命中，而错误命中比不命中贵得多——它会安静地返回
    一个"看起来对"的答案。所以宁可多放：`max_tokens` 会影响截断，
    `provider` 会影响同名模型的不同部署，`request_fingerprint` 装的是
    `enable_thinking` 这类改输出但不改 messages 的请求参数。
    """

    payload = {
        "v": 1,
        "tier": tier,
        "provider": provider,
        "model": model,
        "request": request_fingerprint,
        "max_tokens": max_tokens,
        # 浮点直接进 JSON 会有 0.1 vs 0.1000000001 的表示差异, 定死小数位。
        "temperature": f"{temperature:.4f}",
        "messages": [[item.role, item.content] for item in messages],
    }
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def is_cacheable(*, temperature: float, mode: str) -> bool:
    return temperature == 0.0 and mode != "evaluation"


class RedisCompletionCache:
    """Redis 实现。缓存不可用时一律降级成"未命中"，绝不让它挡住主链路。"""

    def __init__(self, client: Redis, *, prefix: str = "workpilot:llm-cache") -> None:
        self._client = client
        self._prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    async def get(self, key: str) -> CompletionResult | None:
        try:
            raw = await self._client.get(self._key(key))
        except RedisError as error:
            # 缓存是纯优化。Redis 抖一下就让问答失败, 是把可用性押在优化项上。
            logger.warning("精确缓存读取失败, 按未命中继续", error=str(error))
            return None
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            return CompletionResult(
                text=payload["text"],
                model=payload["model"],
                provider=payload["provider"],
                usage=Usage(
                    input_tokens=int(payload["input_tokens"]),
                    output_tokens=int(payload["output_tokens"]),
                ),
            )
        except (ValueError, KeyError, TypeError) as error:
            # 换了序列化格式的旧条目: 当作未命中, 不要让脏数据传染到答案里。
            logger.warning("精确缓存条目无法解析, 按未命中继续", error=str(error))
            return None

    async def set(self, key: str, result: CompletionResult, *, ttl_s: int) -> None:
        payload = json.dumps(
            {
                "text": result.text,
                "model": result.model,
                "provider": result.provider,
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            await self._client.set(self._key(key), payload, ex=ttl_s)
        except RedisError as error:
            logger.warning("精确缓存写入失败, 忽略", error=str(error))
