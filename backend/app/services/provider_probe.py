"""Provider 连接探测与模型目录发现。"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any

import httpx

from app.services.provider_profiles import ProviderProfileRecord


@dataclass(frozen=True)
class ProviderProbeResult:
    models: list[str]
    latency_ms: int


async def probe_provider_profile(
    profile: ProviderProfileRecord,
    *,
    api_key: str,
    timeout_s: float,
    trust_env: bool,
    client: httpx.AsyncClient | None = None,
) -> ProviderProbeResult:
    """只读取模型目录，不发送推理请求，也不产生模型调用费用。"""

    headers: dict[str, str]
    params: dict[str, str] = {}
    if profile.provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    elif profile.provider == "gemini":
        headers = {"x-goog-api-key": api_key}
        params["pageSize"] = "200"
    else:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    owns_client = client is None
    runtime_client = client or httpx.AsyncClient(
        base_url=profile.base_url.rstrip("/") + "/",
        timeout=timeout_s,
        trust_env=trust_env,
    )
    started = monotonic()
    try:
        response = await runtime_client.get("models", headers=headers, params=params)
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            await runtime_client.aclose()
    models = _parse_models(payload, gemini=profile.provider == "gemini")
    elapsed = max(0, round((monotonic() - started) * 1000))
    return ProviderProbeResult(models=models[:200], latency_ms=elapsed)


def _parse_models(payload: Any, *, gemini: bool) -> list[str]:
    if not isinstance(payload, dict):
        raise ValueError("Provider 模型目录响应不是 object")
    raw_items = payload.get("models" if gemini else "data")
    if not isinstance(raw_items, list):
        raise ValueError("Provider 模型目录响应缺少模型数组")
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name" if gemini else "id")
        if not isinstance(raw_name, str):
            continue
        name = raw_name.removeprefix("models/") if gemini else raw_name
        name = name.strip()
        if name and name not in seen:
            result.append(name)
            seen.add(name)
    return sorted(result, key=str.casefold)
