"""Anthropic Messages API adapter using structured outputs (``output_config.format``)."""

from __future__ import annotations

import json
import time
from typing import Any

import anthropic
import httpx2
from django.conf import settings

from apps.llm.base import (
    AuthenticationFailed,
    InvalidResponse,
    LLMError,
    LLMResponse,
    ModelNotFound,
    ProviderUnavailable,
    RateLimited,
    TokenUsage,
    parse_json_text,
)


def _map_error(exc: Exception) -> LLMError:
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return AuthenticationFailed(f"Provider rejected the API key ({exc.status_code}).")
    if isinstance(exc, anthropic.NotFoundError):
        return ModelNotFound("Model not found.")
    if isinstance(exc, anthropic.RateLimitError):
        return RateLimited("Provider rate limit exceeded.")
    if isinstance(exc, anthropic.APITimeoutError | anthropic.APIConnectionError):
        return ProviderUnavailable(f"Could not reach provider: {type(exc).__name__}.")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500 or exc.status_code == 529:
            return ProviderUnavailable(f"Provider error ({exc.status_code}).")
        return LLMError(f"Provider rejected the request ({exc.status_code}): {str(exc.message)[:300]}")
    return LLMError(f"Unexpected provider error: {type(exc).__name__}")


class AnthropicProvider:
    name = "anthropic"
    strict_schema = True

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        http_client: httpx2.Client | None = None,
    ):
        self.model = model
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": settings.LLM_TIMEOUT_SECONDS,
            "max_retries": settings.LLM_MAX_RETRIES,
        }
        if base_url:
            kwargs["base_url"] = base_url
        if http_client is not None:
            kwargs["http_client"] = http_client
        self.client = anthropic.Anthropic(**kwargs)

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_output_tokens: int
    ) -> LLMResponse:
        started = time.monotonic()
        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=max_output_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.AnthropicError as exc:
            raise _map_error(exc) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        usage = TokenUsage(
            input_tokens=getattr(message.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(message.usage, "output_tokens", 0) or 0,
        )
        text = "".join(
            block.text for block in message.content if isinstance(block, anthropic.types.TextBlock)
        )
        if message.stop_reason == "refusal":
            raise InvalidResponse("Model declined to review this content.", usage=usage, raw_text=text)
        if message.stop_reason == "max_tokens":
            raise InvalidResponse(
                "Model output was truncated (max tokens reached).", usage=usage, raw_text=text
            )
        try:
            data = parse_json_text(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise InvalidResponse(
                f"Model did not return valid JSON: {exc}", usage=usage, raw_text=text
            ) from exc
        return LLMResponse(data=data, raw_text=text, usage=usage, model=self.model, latency_ms=latency_ms)

    def validate(self) -> None:
        try:
            self.client.models.retrieve(self.model)
        except anthropic.AnthropicError as exc:
            raise _map_error(exc) from exc
