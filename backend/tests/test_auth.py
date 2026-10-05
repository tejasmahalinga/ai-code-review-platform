from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User

pytestmark = pytest.mark.django_db


def test_setup_status_reports_needs_setup(anon):
    assert anon.get("/api/v1/setup/status").json() == {"needs_setup": True}


def test_setup_creates_first_admin_and_logs_in(anon):
    response = anon.post(
        "/api/v1/setup",
        {"email": "Owner@Example.com", "password": "a-very-long-passphrase"},
        format="json",
    )
    assert response.status_code == 201
    assert response.json()["email"] == "owner@example.com"
    assert response.json()["role"] == "admin"
    assert anon.get("/api/v1/auth/me").status_code == 200


def test_setup_unavailable_once_a_user_exists(anon, admin_user):
    response = anon.post(
        "/api/v1/setup", {"email": "x@example.com", "password": "a-very-long-passphrase"}, format="json"
    )
    assert response.status_code == 404
    assert User.objects.count() == 1


def test_setup_rejects_weak_password(anon):
    response = anon.post("/api/v1/setup", {"email": "x@example.com", "password": "1234567890"}, format="json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_login_logout_flow(anon, admin_user):
    response = anon.post(
        "/api/v1/auth/login",
        {"email": "ADMIN@example.com", "password": "correct-horse-battery"},
        format="json",
    )
    assert response.status_code == 200
    assert anon.get("/api/v1/auth/me").json()["email"] == "admin@example.com"
    assert anon.post("/api/v1/auth/logout").status_code == 204
    assert anon.get("/api/v1/auth/me").status_code == 401


def test_login_wrong_password(anon, admin_user):
    response = anon.post(
        "/api/v1/auth/login", {"email": "admin@example.com", "password": "nope"}, format="json"
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_sixth_failed_login_in_a_minute_is_throttled(anon, admin_user):
    for _ in range(5):
        r = anon.post("/api/v1/auth/login", {"email": "admin@example.com", "password": "nope"}, format="json")
        assert r.status_code == 400
    r = anon.post("/api/v1/auth/login", {"email": "admin@example.com", "password": "nope"}, format="json")
    assert r.status_code == 429


def test_successful_logins_are_not_throttled(anon, admin_user):
    for _ in range(8):
        r = anon.post(
            "/api/v1/auth/login",
            {"email": "admin@example.com", "password": "correct-horse-battery"},
            format="json",
        )
        assert r.status_code == 200


@pytest.mark.parametrize(
    "path",
    ["/api/v1/auth/me", "/api/v1/llm-credentials", "/api/v1/llm-providers"],
)
def test_api_requires_authentication(anon, path):
    assert anon.get(path).status_code == 401


def test_unsafe_requests_require_csrf_token(admin_user):
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(admin_user)
    response = client.post("/api/v1/llm-credentials", {"name": "x"}, format="json")
    assert response.status_code == 403
    client.get("/api/v1/auth/csrf")
    token = client.cookies["csrftoken"].value
    response = client.post(
        "/api/v1/llm-credentials",
        {"name": "Demo", "provider": "fake", "default_model": "demo"},
        format="json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 201


def test_health_endpoints(anon):
    assert anon.get("/healthz").json() == {"status": "ok"}
    ready = anon.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"database": "ok", "cache": "ok"}
