from __future__ import annotations

import io
import logging

import pytest
from django.db import connection

from apps.core.logging import get_logger
from apps.credentials import crypto
from apps.credentials.models import LLMCredential
from tests.http import MockRouter

pytestmark = pytest.mark.django_db

PLAINTEXT = "sk-test-0123456789abcdefghijklmnop"


@pytest.fixture
def openai_mock(monkeypatch):
    """Routes credential validation for the real OpenAI adapter through a mock transport."""
    router = MockRouter()
    from apps.credentials import services

    original = services.build_provider

    def build(provider, **kwargs):
        kwargs["http_client"] = router.client()
        return original(provider, **kwargs)

    monkeypatch.setattr(services, "build_provider", build)
    return router


def create_openai(api, openai_mock, model="gpt-5-mini"):
    openai_mock.add("GET", rf"/v1/models/{model}$", {"id": model, "object": "model"})
    return api.post(
        "/api/v1/llm-credentials",
        {"name": "Main", "provider": "openai", "api_key": PLAINTEXT, "default_model": model},
        format="json",
    )


def test_create_credential_encrypts_secret_and_hides_it(api, openai_mock):
    response = create_openai(api, openai_mock)
    assert response.status_code == 201, response.json()
    data = response.json()
    assert data["last4"] == "mnop"
    assert data["status"] == "valid"
    assert PLAINTEXT not in response.content.decode()
    assert "api_key" not in data

    credential = LLMCredential.objects.get(pk=data["id"])
    assert credential.get_secret() == PLAINTEXT
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT encrypted_secret FROM credentials_llmcredential WHERE id = %s", [credential.pk]
        )
        raw = cursor.fetchone()[0]
    assert PLAINTEXT not in raw
    # The validation request used the key as a bearer token.
    assert openai_mock.requests[0].headers["authorization"] == f"Bearer {PLAINTEXT}"


def test_list_and_detail_never_expose_secret(api, openai_mock):
    create_openai(api, openai_mock)
    listing = api.get("/api/v1/llm-credentials")
    assert listing.status_code == 200
    assert PLAINTEXT not in listing.content.decode()
    assert listing.json()[0]["last4"] == "mnop"


def test_invalid_key_is_rejected_and_not_saved(api, openai_mock):
    openai_mock.add("GET", r"/v1/models/", {"error": {"message": "Incorrect API key"}}, status=401)
    response = api.post(
        "/api/v1/llm-credentials",
        {"name": "Bad", "provider": "openai", "api_key": PLAINTEXT, "default_model": "gpt-5"},
        format="json",
    )
    assert response.status_code == 400
    assert "rejected the API key" in response.json()["error"]["message"]
    assert PLAINTEXT not in response.content.decode()
    assert LLMCredential.objects.count() == 0


def test_unknown_model_is_rejected(api, openai_mock):
    openai_mock.add("GET", r"/v1/models/", {"error": {"message": "not found"}}, status=404)
    response = api.post(
        "/api/v1/llm-credentials",
        {"name": "Bad", "provider": "openai", "api_key": PLAINTEXT, "default_model": "nope"},
        format="json",
    )
    assert response.status_code == 400
    assert "not found" in response.json()["error"]["message"].lower()


def test_api_key_required_for_openai(api):
    response = api.post(
        "/api/v1/llm-credentials",
        {"name": "x", "provider": "openai", "default_model": "gpt-5"},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["details"]["api_key"]


def test_base_url_required_for_openai_compatible(api):
    response = api.post(
        "/api/v1/llm-credentials",
        {"name": "x", "provider": "openai_compatible", "default_model": "llama3.1:8b"},
        format="json",
    )
    assert response.status_code == 400
    assert "base_url" in response.json()["error"]["details"]


def test_api_key_cannot_be_changed_by_patch(api, fake_credential):
    response = api.patch(f"/api/v1/llm-credentials/{fake_credential.pk}", {"api_key": "new"}, format="json")
    assert response.status_code == 400


def test_revoke_wipes_secret_and_hides_credential(api, fake_credential):
    assert api.delete(f"/api/v1/llm-credentials/{fake_credential.pk}").status_code == 204
    fake_credential.refresh_from_db()
    assert fake_credential.status == "revoked"
    assert fake_credential.encrypted_secret == ""
    assert api.get("/api/v1/llm-credentials").json() == []
    assert len(api.get("/api/v1/llm-credentials?include_revoked=true").json()) == 1
    assert api.post(f"/api/v1/llm-credentials/{fake_credential.pk}/validate").status_code == 404


def test_revalidate_marks_invalid(api, admin_user):
    credential = LLMCredential(name="d", provider="fake", default_model="demo", created_by=admin_user)
    credential.set_secret("invalid")
    credential.save()
    response = api.post(f"/api/v1/llm-credentials/{credential.pk}/validate")
    assert response.status_code == 200
    assert response.json()["status"] == "invalid"


def test_providers_endpoint(api):
    ids = [p["id"] for p in api.get("/api/v1/llm-providers").json()]
    assert {"openai", "anthropic", "openai_compatible"} <= set(ids)


def test_crypto_round_trip_and_rotation(settings):
    token = crypto.encrypt("hello")
    assert crypto.decrypt(token) == "hello"
    new_key = "bmV3LWtleS1uZXcta2V5LW5ldy1rZXktbmV3LWtleSE="
    settings.REVIEWBOT_ENCRYPTION_KEYS = [new_key, *settings.REVIEWBOT_ENCRYPTION_KEYS]
    crypto.reset_cache()
    rotated = crypto.rotate(token)
    settings.REVIEWBOT_ENCRYPTION_KEYS = [new_key]
    crypto.reset_cache()
    try:
        assert crypto.decrypt(rotated) == "hello"
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt(token)
    finally:
        crypto.reset_cache()


def test_missing_encryption_key_fails_loudly(settings):
    from django.core.exceptions import ImproperlyConfigured

    settings.REVIEWBOT_ENCRYPTION_KEYS = []
    crypto.reset_cache()
    try:
        with pytest.raises(ImproperlyConfigured):
            crypto.encrypt("x")
    finally:
        crypto.reset_cache()


def test_logs_redact_secrets():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.getLogger().handlers[0].formatter)
    logging.getLogger().addHandler(handler)
    try:
        get_logger("test").warning("leak check", api_key=PLAINTEXT, note=f"value {PLAINTEXT} inline")
        get_logger("test").warning(
            "pem", blob="-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
        )
    finally:
        logging.getLogger().removeHandler(handler)
    output = stream.getvalue()
    assert PLAINTEXT not in output
    assert "BEGIN RSA PRIVATE KEY" not in output
    assert "[REDACTED]" in output


def test_rotate_encryption_key_command(settings, fake_credential):
    from django.core.management import call_command

    from tests.factories import make_connection

    connection = make_connection()
    new_key = "bmV3LWtleS1uZXcta2V5LW5ldy1rZXktbmV3LWtleSE="
    old_keys = list(settings.REVIEWBOT_ENCRYPTION_KEYS)
    settings.REVIEWBOT_ENCRYPTION_KEYS = [new_key, *old_keys]
    crypto.reset_cache()
    try:
        call_command("rotate_encryption_key", stdout=io.StringIO())
        settings.REVIEWBOT_ENCRYPTION_KEYS = [new_key]
        crypto.reset_cache()
        fake_credential.refresh_from_db()
        connection.refresh_from_db()
        assert fake_credential.get_secret() == "fake-key-123456"
        assert connection.webhook_secret == "whsec-test-secret"
        assert "PRIVATE KEY" in connection.private_key
    finally:
        settings.REVIEWBOT_ENCRYPTION_KEYS = old_keys
        crypto.reset_cache()
