"""精确缓存（docs/07 §6 第一层）。

命中率注定不高——同一个人不会逐字重复提问。但它的成本是**零**：命中就省下一整次
推理，没命中只多一次进程内字典查找。§6 里真正危险的是语义缓存（相似 query 不等于
相同意图，`"A 的优点"` 和 `"A 的缺点"` 向量相似度很高），那一层在 Backlog，这里不做。

实现是**进程内 LRU + TTL**，不是 Redis。原来那句"成本是零"建立在 Redis 反正已经
为队列跑着的前提上；队列改成进程内之后，为一个明知命中率低的优化项单养一个外部
依赖（或者一个 SQLite 库文件）就不再是零成本了。代价是重启即失效——对一个每天用
几十次的本机工具，这跟 24 小时 TTL 的实际差别很小。

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
import threading
from collections import OrderedDict
from time import monotonic
from typing import Protocol

from workpilot_ai.types import CompletionResult, Message


class CompletionCache(Protocol):
    async def get(self, key: str) -> CompletionResult | None: ...

    async def set(self, key: str, result: CompletionResult, *, ttl_s: int) -> None: ...


def completion_cache_key(
    *,
    tier: str,
    model: str,
    provider: str,
    messages: list[Message],
    max_tokens: int | None,
    temperature: float,
    request_fingerprint: str = "",
) -> str:
    """把决定输出的全部输入摊平成一个 sha256。

    少放一个字段就是一次错误命中，而错误命中比不命中贵得多——它会安静地返回
    一个"看起来对"的答案。所以宁可多放：`max_tokens` 会影响截断，`None`
    表示让 Provider 使用自身输出边界；`provider` 会影响同名模型的不同部署，
    `request_fingerprint` 装的是
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
        "messages": [
            [
                item.role,
                item.content,
                [attachment.sha256 for attachment in item.attachments],
            ]
            for item in messages
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def is_cacheable(*, temperature: float, mode: str) -> bool:
    return temperature == 0.0 and mode != "evaluation"


class InProcessCompletionCache:
    """进程内 LRU + TTL。

    **必须是进程级单例**（见 `shared_completion_cache`）。每次建网关都新建一个，
    等于每次都是空缓存——那不是"命中率低"，是恒不命中，而调用方无从察觉。

    存 `CompletionResult` 对象本身，不做 JSON 往返：它是 frozen dataclass，取出来
    改不动，序列化只是白花开销。顺带没有了"换了序列化格式的旧条目"那类脏数据——
    进程重启就没有旧条目。

    LRU 上限是必须的：Redis 有 maxmemory 策略兜底，进程内没有，不封顶就是一条
    随运行时长单调增长的内存曲线。
    """

    def __init__(self, *, max_entries: int = 512) -> None:
        if max_entries < 1:
            raise ValueError("max_entries 必须为正")
        self._max_entries = max_entries
        self._entries: OrderedDict[str, tuple[float, CompletionResult]] = OrderedDict()
        self._lock = threading.Lock()

    async def get(self, key: str) -> CompletionResult | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, result = entry
            if expires_at <= monotonic():
                # 过期条目顺手删掉：这是唯一的清扫时机，没有后台线程扫它。
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return result

    async def set(self, key: str, result: CompletionResult, *, ttl_s: int) -> None:
        with self._lock:
            self._entries[key] = (monotonic() + ttl_s, result)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_shared: InProcessCompletionCache | None = None
_shared_lock = threading.Lock()


def shared_completion_cache(*, max_entries: int = 512) -> InProcessCompletionCache:
    """进程内唯一实例。首次调用决定容量，之后的 max_entries 被忽略。"""

    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = InProcessCompletionCache(max_entries=max_entries)
    return _shared
