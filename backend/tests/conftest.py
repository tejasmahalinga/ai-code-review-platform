from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.credentials.models import LLMCredential


@pytest.fixture
def admin_user(db) -> User:
    return User.objects.create_superuser(email="admin@example.com", password="correct-horse-battery")


@pytest.fixture
def api(admin_user) -> APIClient:
    client = APIClient()
    client.force_login(admin_user)
    return client


@pytest.fixture
def anon() -> APIClient:
    return APIClient()


@pytest.fixture
def fake_credential(db, admin_user) -> LLMCredential:
    credential = LLMCredential(name="Demo", provider="fake", default_model="demo", created_by=admin_user)
    credential.set_secret("fake-key-123456")
    credential.save()
    return credential


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()
