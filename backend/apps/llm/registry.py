from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings

from apps.llm.anthropic_provider import AnthropicProvider
from apps.llm.base import LLMProvider
from apps.llm.fake_provider import FakeProvider
from apps.llm.openai_provider import OpenAICompatibleProvider, OpenAIProvider


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    label: str
    requires_api_key: bool
    requires_base_url: bool
    suggested_models: tuple[str, ...]
    default_base_url: str = ""


PROVIDERS: dict[str, tuple[ProviderInfo, type[Any]]] = {
    "openai": (
        ProviderInfo(
            id="openai",
            label="OpenAI",
            requires_api_key=True,
            requires_base_url=False,
            suggested_models=("gpt-5", "gpt-5-mini"),
            default_base_url="https://api.openai.com/v1",
        ),
        OpenAIProvider,
    ),
    "anthropic": (
        ProviderInfo(
            id="anthropic",
            label="Anthropic",
            requires_api_key=True,
            requires_base_url=False,
            suggested_models=("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"),
            default_base_url="https://api.anthropic.com",
        ),
        AnthropicProvider,
    ),
    "openai_compatible": (
        ProviderInfo(
            id="openai_compatible",
            label="OpenAI-compatible (Ollama, vLLM, LM Studio, OpenRouter, ...)",
            requires_api_key=False,
            requires_base_url=True,
            suggested_models=("qwen2.5-coder:14b", "llama3.1:8b"),
            default_base_url="http://localhost:11434/v1",
        ),
        OpenAICompatibleProvider,
    ),
    "fake": (
        ProviderInfo(
            id="fake",
            label="Demo (offline, deterministic)",
            requires_api_key=False,
            requires_base_url=False,
            suggested_models=("demo",),
        ),
        FakeProvider,
    ),
}


def enabled_providers() -> list[ProviderInfo]:
    return [
        info
        for key, (info, _) in PROVIDERS.items()
        if key != "fake" or settings.REVIEWBOT_ENABLE_FAKE_PROVIDER
    ]


def is_enabled(provider: str) -> bool:
    return any(info.id == provider for info in enabled_providers())


def build_provider(
    provider: str, *, api_key: str, model: str, base_url: str | None = None, http_client: Any = None
) -> LLMProvider:
    if not is_enabled(provider):
        raise ValueError(f"Unknown or disabled provider: {provider}")
    _, cls = PROVIDERS[provider]
    instance: LLMProvider = cls(
        api_key=api_key, model=model, base_url=base_url or None, http_client=http_client
    )
    return instance
