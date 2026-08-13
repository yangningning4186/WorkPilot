import hashlib
import math
import re
from collections.abc import AsyncIterator

from app.llm.types import CompletionResult, EmbeddingResult, Message, Usage


class DeterministicProvider:
    name = "deterministic_test"
    chat_model = "fake-chat"
    embedding_model = "fake-embedding"

    def __init__(self, dimensions: int = 1024, completion_text: str | None = None) -> None:
        self.dimensions = dimensions
        self.completion_text = completion_text
        self.last_messages: list[Message] = []

    async def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        del max_tokens, temperature
        self.last_messages = messages
        return CompletionResult(
            text=self.completion_text if self.completion_text is not None else messages[-1].content,
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
        del max_tokens, temperature
        yield messages[-1].content

    async def embed(self, texts: list[str]) -> EmbeddingResult:
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
