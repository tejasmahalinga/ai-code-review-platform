"""OpenAI and OpenAI-compatible (Ollama, vLLM, LM Studio, OpenRouter, gateways) adapter."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx2
import openai
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

OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"


def _map_error(exc: Exception) -> LLMError:
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return AuthenticationFailed(f"Provider rejected the API key ({exc.status_code}).")
    if isinstance(exc, openai.NotFoundError):
        return ModelNotFound("Model or endpoint not found.")
    if isinstance(exc, openai.RateLimitError):
        return RateLimited("Provider rate limit exceeded.")
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return ProviderUnavailable(f"Could not reach provider: {type(exc).__name__}.")
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500:
            return ProviderUnavailable(f"Provider error ({exc.status_code}).")
        return LLMError(f"Provider rejected the request ({exc.status_code}): {_safe_message(exc)}")
    return LLMError(f"Unexpected provider error: {type(exc).__name__}")


def _safe_message(exc: openai.APIStatusError) -> str:
    body = exc.body
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:300]
    return str(exc.message)[:300]


class OpenAIProvider:
    """OpenAI Chat Completions with strict JSON-schema structured output."""

    name = "openai"
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
        self.client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url or OPENAI_DEFAULT_BASE_URL,
            timeout=settings.LLM_TIMEOUT_SECONDS,
            max_retries=settings.LLM_MAX_RETRIES,
            http_client=http_client,
        )

    def _request_kwargs(self, schema: dict[str, Any], max_output_tokens: int) -> dict[str, Any]:
        return {
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "code_review", "schema": schema, "strict": True},
            },
            "max_completion_tokens": max_output_tokens,
        }

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_output_tokens: int
    ) -> LLMResponse:
        started = time.monotonic()
        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                **self._request_kwargs(schema, max_output_tokens),
            )
        except openai.OpenAIError as exc:
            raise _map_error(exc) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        usage = TokenUsage(
            input_tokens=getattr(completion.usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(completion.usage, "completion_tokens", 0) or 0,
        )
        if not completion.choices:
            raise InvalidResponse("Provider returned no choices.", usage=usage)
        choice = completion.choices[0]
        message = choice.message
        if getattr(message, "refusal", None):
            raise InvalidResponse(f"Model refused: {str(message.refusal)[:200]}", usage=usage)
        text = message.content or ""
        if choice.finish_reason == "length":
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
        except openai.OpenAIError as exc:
            raise _map_error(exc) from exc


class OpenAICompatibleProvider(OpenAIProvider):
    """Any server implementing the OpenAI Chat Completions API.

    JSON-schema response formats are unevenly supported across servers, so this adapter requests
    plain JSON mode and relies on the prompt plus client-side schema validation.
    """

    name = "openai_compatible"
    strict_schema = False

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        http_client: httpx2.Client | None = None,
    ):
        if not base_url:
            raise ValueError("base_url is required for OpenAI-compatible providers")
        super().__init__(
            api_key=api_key or "not-needed", model=model, base_url=base_url, http_client=http_client
        )

    def _request_kwargs(self, schema: dict[str, Any], max_output_tokens: int) -> dict[str, Any]:
        return {"response_format": {"type": "json_object"}, "max_tokens": max_output_tokens}

    def validate(self) -> None:
        try:
            models = self.client.models.list()
            ids = [m.id for m in models.data]
        except openai.NotFoundError:
            return  # Server has no /models endpoint; nothing more we can check cheaply.
        except openai.OpenAIError as exc:
            raise _map_error(exc) from exc
        if ids and self.model not in ids:
            raise ModelNotFound(f"Model '{self.model}' is not served by this endpoint.")
