from __future__ import annotations

import json

import pytest

from apps.llm.anthropic_provider import AnthropicProvider
from apps.llm.base import AuthenticationFailed, InvalidResponse, ModelNotFound, RateLimited, parse_json_text
from apps.llm.openai_provider import OpenAICompatibleProvider, OpenAIProvider
from tests.http import MockRouter, body

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}
ANSWER = {"summary": "ok", "findings": []}


def openai_completion(content: str, finish_reason: str = "stop") -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-5-mini",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, "refusal": None},
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    }


def anthropic_message(text: str, stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 30},
    }


@pytest.fixture(autouse=True)
def _no_retries(settings):
    settings.LLM_MAX_RETRIES = 0


def test_openai_and_anthropic_normalize_identically():
    router = MockRouter()
    router.add("POST", r"api\.openai\.com/v1/chat/completions", openai_completion(json.dumps(ANSWER)))
    router.add("POST", r"api\.anthropic\.com/v1/messages", anthropic_message(json.dumps(ANSWER)))

    o = OpenAIProvider(api_key="k", model="gpt-5-mini", http_client=router.client())
    a = AnthropicProvider(api_key="k", model="claude-opus-5-5", http_client=router.client())
    ro = o.complete_json(system="s", user="u", schema=SCHEMA, max_output_tokens=100)
    ra = a.complete_json(system="s", user="u", schema=SCHEMA, max_output_tokens=100)
    assert ro.data == ra.data == ANSWER
    assert (
        (ro.usage.input_tokens, ro.usage.output_tokens)
        == (ra.usage.input_tokens, ra.usage.output_tokens)
        == (120, 30)
    )


def test_openai_request_uses_strict_json_schema():
    router = MockRouter()
    route = router.add("POST", r"/chat/completions", openai_completion(json.dumps(ANSWER)))
    OpenAIProvider(api_key="k", model="gpt-5", http_client=router.client()).complete_json(
        system="sys", user="usr", schema=SCHEMA, max_output_tokens=500
    )
    sent = body(route.calls[0])
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert sent["messages"][0] == {"role": "system", "content": "sys"}
    assert sent["max_completion_tokens"] == 500


def test_anthropic_request_uses_output_config_format():
    router = MockRouter()
    route = router.add("POST", r"/v1/messages", anthropic_message(json.dumps(ANSWER)))
    AnthropicProvider(api_key="k", model="claude-opus-5-5", http_client=router.client()).complete_json(
        system="sys", user="usr", schema=SCHEMA, max_output_tokens=500
    )
    sent = body(route.calls[0])
    assert sent["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert sent["system"] == "sys"
    assert route.calls[0].headers["x-api-key"] == "k"


def test_openai_compatible_uses_json_mode_and_custom_base_url():
    router = MockRouter()
    route = router.add(
        "POST", r"ollama\.local:11434/v1/chat/completions", openai_completion(json.dumps(ANSWER))
    )
    provider = OpenAICompatibleProvider(
        api_key="", model="qwen", base_url="http://ollama.local:11434/v1", http_client=router.client()
    )
    result = provider.complete_json(system="s", user="u", schema=SCHEMA, max_output_tokens=100)
    assert result.data == ANSWER
    assert body(route.calls[0])["response_format"] == {"type": "json_object"}


def test_invalid_json_raises_invalid_response_with_raw_text():
    router = MockRouter()
    router.add("POST", r"/chat/completions", openai_completion("not json at all"))
    with pytest.raises(InvalidResponse) as exc:
        OpenAIProvider(api_key="k", model="m", http_client=router.client()).complete_json(
            system="s", user="u", schema=SCHEMA, max_output_tokens=10
        )
    assert exc.value.raw_text == "not json at all"
    assert exc.value.usage.input_tokens == 120


def test_truncated_output_is_invalid():
    router = MockRouter()
    router.add("POST", r"/v1/messages", anthropic_message('{"summary": "x', stop_reason="max_tokens"))
    with pytest.raises(InvalidResponse, match="truncated"):
        AnthropicProvider(api_key="k", model="m", http_client=router.client()).complete_json(
            system="s", user="u", schema=SCHEMA, max_output_tokens=10
        )


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, AuthenticationFailed), (403, AuthenticationFailed), (404, ModelNotFound), (429, RateLimited)],
)
def test_error_mapping(status, error):
    router = MockRouter()
    router.add("POST", r"/chat/completions", {"error": {"message": "x"}}, status=status)
    router.add(
        "POST", r"/v1/messages", {"type": "error", "error": {"type": "x", "message": "x"}}, status=status
    )
    with pytest.raises(error):
        OpenAIProvider(api_key="k", model="m", http_client=router.client()).complete_json(
            system="s", user="u", schema=SCHEMA, max_output_tokens=10
        )
    with pytest.raises(error):
        AnthropicProvider(api_key="k", model="m", http_client=router.client()).complete_json(
            system="s", user="u", schema=SCHEMA, max_output_tokens=10
        )


def test_rate_limit_is_retried_honoring_retry_after(settings):
    settings.LLM_MAX_RETRIES = 2
    router = MockRouter()
    attempts = {"n": 0}

    def handler(request):
        import httpx2

        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx2.Response(
                429, json={"error": {"message": "slow down"}}, headers={"retry-after": "0"}
            )
        return httpx2.Response(200, json=openai_completion(json.dumps(ANSWER)))

    router.add("POST", r"/chat/completions", handler=handler)
    result = OpenAIProvider(api_key="k", model="m", http_client=router.client()).complete_json(
        system="s", user="u", schema=SCHEMA, max_output_tokens=10
    )
    assert result.data == ANSWER
    assert attempts["n"] == 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Here you go: {"a": 1} thanks', {"a": 1}),
    ],
)
def test_parse_json_text(text, expected):
    assert parse_json_text(text) == expected
