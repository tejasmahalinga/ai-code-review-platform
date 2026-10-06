"""GitHub App setup (manifest flow), webhook verification, and client factories."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any

from django.conf import settings

from apps.git_providers.github.client import GitHubAppClient, GitHubInstallationClient

# Least privilege: read code, write PR reviews and check runs. "emails" is a user permission used only by
# "Sign in with GitHub" to read the signed-in user's verified addresses.
APP_PERMISSIONS = {
    "pull_requests": "write",
    "contents": "read",
    "metadata": "read",
    "checks": "write",
    "emails": "read",
}
# installation / installation_repositories events are always delivered to Apps.
APP_EVENTS = ["pull_request", "issue_comment"]  # issue_comment carries `/reviewbot` commands


def webhook_url() -> str:
    return f"{settings.PUBLIC_URL}/webhooks/github"


def oauth_callback_url() -> str:
    return f"{settings.PUBLIC_URL}/api/v1/auth/github/callback"


def build_manifest(app_name: str) -> dict[str, Any]:
    base = settings.PUBLIC_URL
    return {
        "name": app_name,
        "url": base,
        "hook_attributes": {"url": webhook_url(), "active": True},
        "redirect_url": f"{base}/api/v1/integrations/github/callback",
        "setup_url": f"{base}/api/v1/integrations/github/installed",
        "setup_on_update": True,
        "callback_urls": [oauth_callback_url()],
        "request_oauth_on_install": False,
        "public": False,
        "default_permissions": APP_PERMISSIONS,
        "default_events": APP_EVENTS,
    }


def manifest_post_url(web_url: str, state: str, organization: str | None) -> str:
    if organization:
        return f"{web_url}/organizations/{organization}/settings/apps/new?state={state}"
    return f"{web_url}/settings/apps/new?state={state}"


def new_state() -> str:
    return secrets.token_urlsafe(24)


def manifest_json(app_name: str) -> str:
    return json.dumps(build_manifest(app_name))


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Validates ``X-Hub-Signature-256`` in constant time."""
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def app_client(connection: Any, http_client: Any = None) -> GitHubAppClient:
    return GitHubAppClient(
        api_url=connection.api_url,
        app_id=connection.app_id,
        private_key=connection.private_key,
        http_client=http_client,
    )


def installation_client(installation: Any, http_client: Any = None) -> GitHubInstallationClient:
    return app_client(installation.connection, http_client).for_installation(installation.external_id)
