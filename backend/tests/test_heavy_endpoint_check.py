from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from eval.heavy_endpoint_check import check_heavy_endpoint


def test_heavy_endpoint_check_records_exact_identity(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "deepseek-v4-flash"}]})
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-flash",
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                "system_fingerprint": "test-vllm",
            },
        )

    client = httpx.AsyncClient(
        base_url="http://heavy.test/v1", transport=httpx.MockTransport(handler)
    )
    output = tmp_path / "report.json"
    result = asyncio.run(
        check_heavy_endpoint(
            output=output,
            chat_smoke=True,
            settings=Settings(
                tier_heavy_base_url="http://heavy.test/v1",
                tier_heavy_model="deepseek-v4-flash",
            ),
            client=client,
        )
    )
    asyncio.run(client.aclose())

    assert result["models_check"]["passed"] is True
    assert result["chat_smoke"]["response_model"] == "deepseek-v4-flash"
    assert json.loads(output.read_text())["data_scope"].startswith("synthetic")


def test_heavy_endpoint_check_rejects_identity_drift(tmp_path: Path) -> None:
    client = httpx.AsyncClient(
        base_url="http://heavy.test/v1",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"data": [{"id": "wrong-model"}]})
        ),
    )
    with pytest.raises(ValueError, match="身份不符"):
        asyncio.run(
            check_heavy_endpoint(
                output=tmp_path / "report.json",
                chat_smoke=False,
                settings=Settings(
                    tier_heavy_base_url="http://heavy.test/v1",
                    tier_heavy_model="deepseek-v4-flash",
                ),
                client=client,
            )
        )
    asyncio.run(client.aclose())
