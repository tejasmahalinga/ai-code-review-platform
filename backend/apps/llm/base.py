"""Provider-neutral LLM interface.

Every adapter turns ``(system, user, schema)`` into a parsed JSON object plus token usage, and maps
provider errors onto the small exception hierarchy below so the review pipeline can decide whether
to retry, fail the run, or mark the credential invalid.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMResponse:
    data: dict[str, Any]
    raw_text: str
    usage: TokenUsage
    model: str
    latency_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class LLMError(Exception):
    """Base error. ``retryable`` tells the caller whether trying again later could succeed."""

    retryable = False
    code = "llm_error"

    def __init__(self, message: str, *, usage: TokenUsage | None = None, raw_text: str = ""):
        super().__init__(message)
        self.usage = usage or TokenUsage()
        self.raw_text = raw_text


class AuthenticationFailed(LLMError):
    code = "invalid_credentials"


class ModelNotFound(LLMError):
    code = "model_not_found"


class RateLimited(LLMError):
    retryable = True
    code = "rate_limited"


class ProviderUnavailable(LLMError):
    retryable = True
    code = "provider_unavailable"


class InvalidResponse(LLMError):
    """The model answered, but not with JSON matching the requested schema (or it refused)."""

    code = "invalid_response"


class LLMProvider(Protocol):
    name: str
    model: str

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_output_tokens: int
    ) -> LLMResponse: ...

    def validate(self) -> None:
        """Cheaply verifies the credential and model. Raises an LLMError subclass on failure."""
        ...


def parse_json_text(text: str) -> dict[str, Any]:
    """Parses a model's JSON answer, tolerating a surrounding Markdown code fence."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found")
    value = json.loads(stripped[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("top-level JSON value is not an object")
    return value
