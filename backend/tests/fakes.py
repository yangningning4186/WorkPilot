import hashlib
import math
import re
from collections.abc import AsyncIterator

from app.agent.state import BudgetState
from app.llm.types import CompletionResult, EmbeddingResult, Message, Usage


class DeterministicProvider:
    name = "deterministic_test"
    chat_model = "fake-chat"
    embedding_model = "fake-embedding"

    def __init__(
        self,
        dimensions: int = 1024,
        completion_text: str | None = None,
        completion_texts: list[str] | None = None,
    ) -> None:
        self.dimensions = dimensions
        self.completion_text = completion_text
        self.completion_texts = list(completion_texts or [])
        self.last_messages: list[Message] = []
        # 用来断言"通用知识模式确实没有检索"——没有 embedding 就没有召回。
        self.embed_calls = 0

    def queue_completions(self, *values: str) -> None:
        self.completion_texts.extend(values)

    async def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        del max_tokens, temperature
        self.last_messages = messages
        text = self.completion_texts.pop(0) if self.completion_texts else self.completion_text
        return CompletionResult(
            text=text if text is not None else messages[-1].content,
            model=self.chat_model,
            provider=self.name,
            usage=Usage(input_tokens=3, output_tokens=2),
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        """与 complete 取同一份排队文本, 只是切成多片吐出来。

        两个入口的行为必须一致: 生成走流式、评测走非流式, 假 provider 一旦分叉,
        测试就只能覆盖其中一条(和线上一样的坑)。分片也是刻意的——一次性吐完整段
        会让"边界正好落在拒答哨兵上"这类流式专属 bug 永远测不出来。
        """

        del max_tokens, temperature
        self.last_messages = messages
        text = self.completion_texts.pop(0) if self.completion_texts else self.completion_text
        content = text if text is not None else messages[-1].content
        for index in range(0, len(content), 8):
            yield content[index : index + 8]

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.embed_calls += 1
        return EmbeddingResult(
            embeddings=[self._vector(text) for text in texts],
            model=self.embedding_model,
            provider=self.name,
            usage=Usage(input_tokens=sum(len(text) for text in texts)),
        )

    async def aclose(self) -> None:
        return None

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = re.findall(r"[\w\u4e00-\u9fff]+", text.casefold())
        # 中文按双字窗口补充特征, 使短查询能命中文档中的相同词组。
        tokens.extend(text[index : index + 2] for index in range(max(0, len(text) - 1)))
        for token in tokens:
            digest = hashlib.sha256(token.encode()).digest()
            slot = int.from_bytes(digest[:4]) % self.dimensions
            vector[slot] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


def review_budget(
    *,
    max_tokens: int = 10_000,
    max_calls: int = 20,
    max_wall_ms: int = 60_000,
    used_tokens: int = 0,
    used_calls: int = 0,
    used_wall_ms: int = 0,
) -> BudgetState:
    """固定综述测试用的预算快照；默认额度宽到不会误触熔断。"""

    return {
        "max_tokens": max_tokens,
        "used_tokens": used_tokens,
        "max_calls": max_calls,
        "used_calls": used_calls,
        "max_wall_ms": max_wall_ms,
        "used_wall_ms": used_wall_ms,
        "started_at_ms": 0,
    }


class FrozenClock:
    """手控毫秒时钟；墙钟熔断不能靠 sleep 去等真实时间。"""

    def __init__(self, now_ms: int = 0) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, delta_ms: int) -> None:
        self.now_ms += delta_ms
