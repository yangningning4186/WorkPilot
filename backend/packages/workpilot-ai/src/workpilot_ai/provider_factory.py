"""用户 Provider profile 到统一 ModelProvider 的唯一构造入口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from workpilot_ai.providers.anthropic import AnthropicProvider
from workpilot_ai.providers.gemini import GeminiProvider
from workpilot_ai.providers.openai_compatible import OpenAICompatibleProvider
from workpilot_ai.types import ModelProvider

ProviderKind = Literal[
    "openai",
    "anthropic",
    "gemini",
    "deepseek",
    "qwen",
    "ollama",
    "openai_compatible",
]


@dataclass(frozen=True)
class ChatProviderConfig:
    provider: ProviderKind
    base_url: str
    api_key: str
    model: str
    timeout_s: float
    prompt_cache_key_supported: bool = False


def build_chat_provider(config: ChatProviderConfig, *, trust_env: bool = False) -> ModelProvider:
    if config.provider == "anthropic":
        return AnthropicProvider(
            base_url=config.base_url,
            api_key=config.api_key,
            chat_model=config.model,
            timeout_s=config.timeout_s,
            trust_env=trust_env,
        )
    if config.provider == "gemini":
        return GeminiProvider(
            base_url=config.base_url,
            api_key=config.api_key,
            chat_model=config.model,
            timeout_s=config.timeout_s,
            trust_env=trust_env,
        )
    return OpenAICompatibleProvider(
        provider_name=config.provider,
        base_url=config.base_url,
        api_key=config.api_key,
        chat_model=config.model,
        embedding_model="conversation-only",
        prompt_cache_key_supported=(
            config.provider == "openai" or config.prompt_cache_key_supported
        ),
        timeout_s=config.timeout_s,
        trust_env=trust_env,
    )
