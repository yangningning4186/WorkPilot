"""用户 Provider profile 到统一 ModelProvider 的唯一构造入口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.llm.providers.anthropic import AnthropicProvider
from app.llm.providers.gemini import GeminiProvider
from app.llm.providers.openai_compatible import OpenAICompatibleProvider
from app.llm.types import ModelProvider

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
        timeout_s=config.timeout_s,
        trust_env=trust_env,
    )
