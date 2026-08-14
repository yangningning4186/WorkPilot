import json

import httpx
import pytest

from app.llm.gateway import EmbeddingDimensionError, EmbeddingIdentityError, ModelGateway
from app.llm.providers.openai_compatible import OpenAICompatibleProvider
from app.llm.types import Message
from tests.fakes import DeterministicProvider


async def test_gateway_exposes_complete_stream_and_embed() -> None:
    gateway = ModelGateway(DeterministicProvider(4), embedding_dimensions=4)

    completion = await gateway.complete([Message(role="user", content="hello")])
    streamed = [part async for part in gateway.stream([Message(role="user", content="world")])]
    embeddings = await gateway.embed(["hello", "world"])

    assert completion.text == "hello"
    assert streamed == ["world"]
    assert len(embeddings.embeddings) == 2
    assert len(embeddings.embeddings[0]) == 4


async def test_gateway_rejects_wrong_embedding_dimensions() -> None:
    gateway = ModelGateway(DeterministicProvider(3), embedding_dimensions=4)
    with pytest.raises(EmbeddingDimensionError):
        await gateway.embed(["hello"])


async def test_gateway_rejects_unexpected_embedding_identity() -> None:
    provider = DeterministicProvider(4)
    provider.embedding_model = "configured-model"
    gateway = ModelGateway(provider, embedding_dimensions=4)
    provider.embedding_model = "changed-model"

    with pytest.raises(EmbeddingIdentityError):
        await gateway.embed(["hello"])


async def test_openai_compatible_provider_maps_wire_format() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "model": "served-chat",
                    "choices": [{"message": {"content": "answer"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                },
            )
        return httpx.Response(
            200,
            json={
                "model": "served-embedding",
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ],
                "usage": {"prompt_tokens": 4},
            },
        )

    client = httpx.AsyncClient(
        base_url="http://model.test/v1", transport=httpx.MockTransport(handler)
    )
    provider = OpenAICompatibleProvider(
        base_url="http://unused.test/v1",
        api_key="secret",
        chat_model="chat",
        embedding_model="embed",
        enable_thinking=False,
        client=client,
    )

    completion = await provider.complete(
        [Message(role="user", content="question")], max_tokens=20, temperature=0.0
    )
    embeddings = await provider.embed(["one", "two"])
    await client.aclose()

    assert completion.text == "answer"
    assert completion.usage.input_tokens == 5
    assert embeddings.embeddings == [[1.0, 0.0], [0.0, 1.0]]
    assert [request.url.path for request in requests] == [
        "/v1/chat/completions",
        "/v1/embeddings",
    ]
    assert requests[0].headers["authorization"] == "Bearer secret"
    assert json.loads(requests[0].content)["chat_template_kwargs"] == {"enable_thinking": False}
    assert json.loads(requests[1].content)["input"] == ["one", "two"]
