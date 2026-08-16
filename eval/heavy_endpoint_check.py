"""验证配置中的 DeepSeek heavy 端点，不发送项目数据。"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.core.config import Settings


async def check_heavy_endpoint(
    *,
    output: Path,
    chat_smoke: bool,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    settings = settings or Settings()
    base_url = settings.tier_heavy_base_url.rstrip("/")
    model = settings.tier_heavy_model.strip()
    if not base_url or not model:
        raise ValueError("TIER_HEAVY_BASE_URL/TIER_HEAVY_MODEL 尚未配置")
    headers = (
        {"Authorization": f"Bearer {settings.cluster_api_key}"}
        if settings.cluster_api_key
        else {}
    )
    owns_client = client is None
    current_client = client or httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=120,
        trust_env=False,
    )
    try:
        models_response = await current_client.get("/models")
        models_response.raise_for_status()
        models_payload = models_response.json()
        model_ids = _model_ids(models_payload)
        if model not in model_ids:
            raise ValueError(f"heavy 模型身份不符: expected={model}, actual={model_ids}")
        chat_result: dict[str, Any] | None = None
        if chat_smoke:
            response = await current_client.post(
                "/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": 'Return exactly: {"status":"ok"}',
                        }
                    ],
                    "temperature": 0,
                    "max_tokens": 64,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("model") != model:
                raise ValueError(
                    "heavy chat 实际身份漂移: "
                    f"expected={model}, actual={payload.get('model')}"
                )
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ValueError("heavy chat smoke 缺少 choices")
            chat_result = {
                "passed": True,
                "response_model": payload["model"],
                "finish_reason": choices[0].get("finish_reason"),
                "system_fingerprint": payload.get("system_fingerprint"),
            }
        result = {
            "schema_version": 1,
            "checked_at": datetime.now(UTC).isoformat(),
            "endpoint": base_url,
            "expected_model": model,
            "models_check": {"passed": True, "model_ids": model_ids},
            "chat_smoke": chat_result,
            "data_scope": "synthetic health prompt only; no project data sent",
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result
    finally:
        if owns_client:
            await current_client.aclose()


def _model_ids(payload: object) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise TypeError("/models 响应格式无效")
    ids = [
        str(item["id"])
        for item in payload["data"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    if not ids:
        raise ValueError("/models 未返回任何模型")
    return ids


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验证配置中的 heavy 模型端点")
    parser.add_argument("--chat-smoke", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("eval/outputs/heavy-endpoint-check/report.json"),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = asyncio.run(
        check_heavy_endpoint(output=args.output, chat_smoke=args.chat_smoke)
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
