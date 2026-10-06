from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication
from rest_framework.authentication import SessionAuthentication as DRFSessionAuthentication
from rest_framework.request import Request

from apps.accounts.models import API_TOKEN_PREFIX, ApiToken, hash_token


class SessionAuthentication(DRFSessionAuthentication):
    """Session auth that answers unauthenticated requests with 401 (not 403)."""

    def authenticate_header(self, request: Request) -> str:
        return 'Session realm="api"'


class TokenAuthentication(BaseAuthentication):
    """``Authorization: Bearer rbt_...`` personal API tokens (ADM-06). No CSRF: tokens are not ambient."""

    keyword = "Bearer"
    LAST_USED_RESOLUTION = timedelta(minutes=1)

    def authenticate(self, request: Request) -> tuple[Any, Any] | None:
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith(f"{self.keyword} {API_TOKEN_PREFIX}"):
            return None
        raw = header[len(self.keyword) + 1 :].strip()
        token = ApiToken.objects.select_related("user").filter(token_hash=hash_token(raw)).first()
        if token is None or not token.is_active or not token.user.is_active:
            raise exceptions.AuthenticationFailed("Invalid or expired API token.")
        now = timezone.now()
        if token.last_used_at is None or now - token.last_used_at > self.LAST_USED_RESOLUTION:
            ApiToken.objects.filter(pk=token.pk).update(last_used_at=now)
        return token.user, token

    def authenticate_header(self, request: Request) -> str:
        return 'Bearer realm="api"'
