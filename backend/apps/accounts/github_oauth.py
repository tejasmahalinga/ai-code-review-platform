"""Sign in with GitHub (ADM-03): OAuth web flow and identity lookup.

Uses the GitHub App's OAuth client (created by the manifest flow) unless a separate OAuth App is configured
with ``REVIEWBOT_GITHUB_OAUTH_CLIENT_ID`` / ``_SECRET``. The user access token is used only to read the
user's profile and verified emails, then discarded; it is never stored or logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx2
from django.conf import settings

from apps.git_providers.github.app import oauth_callback_url
from apps.repositories.models import GitProviderConnection


class OAuthError(Exception):
    pass


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class GitHubIdentity:
    id: int
    login: str
    name: str
    verified_emails: tuple[str, ...]  # primary first

    @property
    def primary_email(self) -> str:
        return self.verified_emails[0] if self.verified_emails else ""


def oauth_client() -> OAuthClient | None:
    if not settings.GITHUB_LOGIN_ENABLED:
        return None
    if settings.GITHUB_OAUTH_CLIENT_ID and settings.GITHUB_OAUTH_CLIENT_SECRET:
        return OAuthClient(settings.GITHUB_OAUTH_CLIENT_ID, settings.GITHUB_OAUTH_CLIENT_SECRET)
    connection = GitProviderConnection.objects.filter(provider=GitProviderConnection.Provider.GITHUB).first()
    if connection is None or not connection.client_id or not connection.encrypted_client_secret:
        return None
    return OAuthClient(connection.client_id, connection.client_secret)


def authorize_url(client: OAuthClient, state: str) -> str:
    query = urlencode(
        {
            "client_id": client.client_id,
            "redirect_uri": oauth_callback_url(),
            "state": state,
            "scope": "read:user user:email",  # ignored for GitHub Apps; used by OAuth Apps
            "allow_signup": "false",
        }
    )
    return f"{settings.GITHUB_URL}/login/oauth/authorize?{query}"


def http_client() -> httpx2.Client:
    return httpx2.Client(timeout=settings.GIT_TIMEOUT_SECONDS)


def _json(response: httpx2.Response, what: str) -> object:
    if response.status_code >= 400:
        raise OAuthError(f"GitHub {what} failed (HTTP {response.status_code}).")
    try:
        return response.json()
    except ValueError as exc:
        raise OAuthError(f"GitHub {what} returned an invalid response.") from exc


def fetch_identity(client: OAuthClient, code: str) -> GitHubIdentity:
    """Exchanges the OAuth code and returns the GitHub user with their verified email addresses."""
    with http_client() as http:
        token_data = _json(
            http.post(
                f"{settings.GITHUB_URL}/login/oauth/access_token",
                headers={"Accept": "application/json"},
                data={
                    "client_id": client.client_id,
                    "client_secret": client.client_secret,
                    "code": code,
                    "redirect_uri": oauth_callback_url(),
                },
            ),
            "token exchange",
        )
        token = token_data.get("access_token") if isinstance(token_data, dict) else None
        if not token:
            error = token_data.get("error", "unknown") if isinstance(token_data, dict) else "unknown"
            raise OAuthError(f"GitHub did not issue a token ({error}).")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        user = _json(http.get(f"{settings.GITHUB_API_URL}/user", headers=headers), "user lookup")
        if not isinstance(user, dict) or not isinstance(user.get("id"), int):
            raise OAuthError("GitHub returned an unexpected user payload.")
        emails: list[str] = []
        response = http.get(f"{settings.GITHUB_API_URL}/user/emails", headers=headers)
        if response.status_code < 400:
            rows = response.json()
            if isinstance(rows, list):
                verified = [r for r in rows if isinstance(r, dict) and r.get("verified") and r.get("email")]
                verified.sort(key=lambda r: not r.get("primary"))
                emails = [str(r["email"]).lower() for r in verified]
        elif user.get("email"):
            # Without the emails permission, fall back to the public profile email (GitHub only allows
            # verified addresses there).
            emails = [str(user["email"]).lower()]
    return GitHubIdentity(
        id=user["id"],
        login=str(user.get("login", "")),
        name=str(user.get("name") or ""),
        verified_emails=tuple(dict.fromkeys(emails)),
    )


def signup_allowed(email: str) -> bool:
    if not settings.ALLOW_SIGNUP or not email:
        return False
    if not settings.SIGNUP_EMAIL_DOMAINS:
        return True
    return email.rsplit("@", 1)[-1].lower() in settings.SIGNUP_EMAIL_DOMAINS
