import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.llm.types import CompletionResult, EmbeddingResult, Message, Usage


class ProviderResponseError(RuntimeError):
    pass


class OpenAICompatibleProvider:
    """覆盖 OpenAI-compatible Chat Completions 与 Embeddings 接口。"""

    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        chat_model: str,
        embedding_model: str,
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.chat_model = chat_model
        self.embedding_model = embedding_model
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)

    async def complete(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        response = await self._client.post(
            "chat/completions",
            headers=self._headers,
            json={
                "model": self.chat_model,
                "messages": [vars(message) for message in messages],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        try:
            text = str(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderResponseError("模型响应缺少 choices[0].message.content") from error
        usage = payload.get("usage") or {}
        return CompletionResult(
            text=text,
            model=str(payload.get("model") or self.chat_model),
            provider=self.name,
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
            ),
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        async with self._client.stream(
            "POST",
            "chat/completions",
            headers=self._headers,
            json={
                "model": self.chat_model,
                "messages": [vars(message) for message in messages],
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": True,
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                payload = json.loads(data)
                delta = payload.get("choices", [{}])[0].get("delta", {}).get("content")
                if delta:
                    yield str(delta)

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        response = await self._client.post(
            "embeddings",
            headers=self._headers,
            json={"model": self.embedding_model, "input": texts},
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        rows = sorted(payload.get("data", []), key=lambda row: int(row["index"]))
        embeddings = [[float(value) for value in row["embedding"]] for row in rows]
        if len(embeddings) != len(texts):
            raise ProviderResponseError("embedding 响应数量与输入不一致")
        usage = payload.get("usage") or {}
        return EmbeddingResult(
            embeddings=embeddings,
            model=str(payload.get("model") or self.embedding_model),
            provider=self.name,
            usage=Usage(input_tokens=int(usage.get("prompt_tokens", 0))),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
