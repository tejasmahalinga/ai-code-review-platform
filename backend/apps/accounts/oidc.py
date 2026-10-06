"""OpenID Connect single sign-on (ADM-07): Okta, Microsoft Entra ID, Keycloak, Google, and GitLab.

Authorization code flow with PKCE (S256), ``state`` and ``nonce``. ID tokens are verified against the
issuer's JWKS (asymmetric algorithms only), ``iss``, ``aud``, ``exp`` and ``nonce``. Group claims can map
users to roles.
Providers are configured with environment variables, so a misconfigured provider can never lock admins out of
the dashboard (password sign-in stays available unless explicitly restricted).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx2
import jwt
from django.conf import settings
from django.core.cache import cache

ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
DISCOVERY_TTL = 3600
ROLE_ORDER = ("admin", "reviewer", "viewer")


class OIDCError(Exception):
    pass


@dataclass(frozen=True)
class OIDCProvider:
    id: str
    name: str
    issuer: str
    client_id: str
    client_secret: str
    scopes: str = "openid email profile"
    groups_claim: str = "groups"
    # (role, groups) from highest to lowest; "*" matches everyone.
    role_mapping: tuple[tuple[str, frozenset[str]], ...] = ()
    sync_roles: bool = True
    trust_email: bool = False

    @property
    def maps_roles(self) -> bool:
        return bool(self.role_mapping)

    def role_for(self, groups: list[str]) -> str | None:
        names = set(groups)
        for role, wanted in self.role_mapping:
            if "*" in wanted or names & wanted:
                return role
        return None


@dataclass(frozen=True)
class OIDCIdentity:
    provider: str
    subject: str
    email: str
    email_verified: bool
    name: str
    username: str
    groups: list[str] = field(default_factory=list)


def parse_role_mapping(raw: str) -> tuple[tuple[str, frozenset[str]], ...]:
    """``admin=platform-admins;reviewer=eng,qa;viewer=*`` → ordered (role, groups) pairs."""
    mapping: dict[str, frozenset[str]] = {}
    for part in filter(None, (p.strip() for p in raw.split(";"))):
        role, _, groups = part.partition("=")
        role = role.strip().lower()
        if role not in ROLE_ORDER:
            raise ValueError(f"Unknown role '{role}' in role mapping; use admin, reviewer or viewer.")
        mapping[role] = frozenset(g.strip() for g in groups.split(",") if g.strip())
    return tuple((role, mapping[role]) for role in ROLE_ORDER if role in mapping)


def providers() -> dict[str, OIDCProvider]:
    """Configured sign-in providers: the generic ``sso`` provider and ``gitlab``."""
    found: dict[str, OIDCProvider] = {}
    if settings.OIDC_ISSUER and settings.OIDC_CLIENT_ID:
        found["sso"] = OIDCProvider(
            id="sso",
            name=settings.OIDC_NAME,
            issuer=settings.OIDC_ISSUER.rstrip("/"),
            client_id=settings.OIDC_CLIENT_ID,
            client_secret=settings.OIDC_CLIENT_SECRET,
            scopes=settings.OIDC_SCOPES,
            groups_claim=settings.OIDC_GROUPS_CLAIM,
            role_mapping=parse_role_mapping(settings.OIDC_ROLE_MAPPING),
            sync_roles=settings.OIDC_SYNC_ROLES,
            trust_email=settings.OIDC_TRUST_EMAIL,
        )
    if settings.GITLAB_OAUTH_CLIENT_ID and settings.GITLAB_OAUTH_CLIENT_SECRET:
        issuer = settings.GITLAB_OAUTH_URL
        if not issuer:
            from apps.repositories.services import gitlab_connection

            connection = gitlab_connection()
            issuer = connection.web_url if connection else "https://gitlab.com"
        found["gitlab"] = OIDCProvider(
            id="gitlab",
            name="GitLab",
            issuer=issuer.rstrip("/"),
            client_id=settings.GITLAB_OAUTH_CLIENT_ID,
            client_secret=settings.GITLAB_OAUTH_CLIENT_SECRET,
            scopes="openid email profile",
            role_mapping=parse_role_mapping(settings.GITLAB_ROLE_MAPPING),
            sync_roles=settings.OIDC_SYNC_ROLES,
        )
    return found


def get_provider(provider_id: str) -> OIDCProvider | None:
    return providers().get(provider_id)


def callback_url(provider: OIDCProvider) -> str:
    return f"{settings.PUBLIC_URL}/api/v1/auth/oidc/{provider.id}/callback"


def http_client() -> httpx2.Client:
    return httpx2.Client(timeout=settings.GIT_TIMEOUT_SECONDS)


def _get_json(url: str) -> dict[str, Any]:
    with http_client() as client:
        response = client.get(url, headers={"Accept": "application/json"})
    if response.status_code >= 400:
        raise OIDCError(f"{url} returned HTTP {response.status_code}")
    data = response.json()
    if not isinstance(data, dict):
        raise OIDCError(f"{url} returned an unexpected document")
    return data


def discovery(provider: OIDCProvider) -> dict[str, Any]:
    key = "oidc:discovery:" + hashlib.sha256(provider.issuer.encode()).hexdigest()
    cached = cache.get(key)
    if cached:
        return cached
    document = _get_json(f"{provider.issuer}/.well-known/openid-configuration")
    if document.get("issuer", "").rstrip("/") != provider.issuer:
        raise OIDCError("The discovery document's issuer does not match the configured issuer.")
    for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not document.get(required):
            raise OIDCError(f"The discovery document has no {required}.")
    cache.set(key, document, DISCOVERY_TTL)
    return document


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(provider: OIDCProvider, *, state: str, nonce: str, code_challenge: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": provider.client_id,
            "redirect_uri": callback_url(provider),
            "scope": provider.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{discovery(provider)['authorization_endpoint']}?{query}"


def _jwks(provider: OIDCProvider, refresh: bool = False) -> jwt.PyJWKSet:
    key = "oidc:jwks:" + hashlib.sha256(provider.issuer.encode()).hexdigest()
    raw = None if refresh else cache.get(key)
    if raw is None:
        raw = _get_json(discovery(provider)["jwks_uri"])
        cache.set(key, raw, DISCOVERY_TTL)
    return jwt.PyJWKSet.from_dict(raw)


def verify_id_token(provider: OIDCProvider, token: str, nonce: str) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise OIDCError("The ID token is malformed.") from exc
    algorithm = header.get("alg")
    if algorithm not in ALLOWED_ALGORITHMS:
        raise OIDCError(f"Unsupported ID token algorithm: {algorithm}")
    kid = header.get("kid")
    signing_key = None
    for refresh in (False, True):  # the IdP may have rotated its keys
        keys = _jwks(provider, refresh=refresh).keys
        signing_key = next((k for k in keys if kid is None or k.key_id == kid), None)
        if signing_key is not None:
            break
    if signing_key is None:
        raise OIDCError("No matching signing key for the ID token.")
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key=signing_key.key,
            algorithms=[algorithm],
            audience=provider.client_id,
            issuer=discovery(provider)["issuer"],
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            leeway=60,
        )
    except jwt.PyJWTError as exc:
        raise OIDCError(f"The ID token is invalid: {exc}") from exc
    if not nonce or not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OIDCError("The ID token nonce does not match.")
    return claims


def fetch_identity(provider: OIDCProvider, *, code: str, code_verifier: str, nonce: str) -> OIDCIdentity:
    meta = discovery(provider)
    with http_client() as client:
        response = client.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": callback_url(provider),
                "code_verifier": code_verifier,
                "client_id": provider.client_id,
            },
            auth=(provider.client_id, provider.client_secret),
            headers={"Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise OIDCError(f"The token exchange failed (HTTP {response.status_code}).")
        tokens = response.json()
        if not isinstance(tokens, dict) or not tokens.get("id_token"):
            raise OIDCError("The identity provider returned no ID token.")
        claims = verify_id_token(provider, str(tokens["id_token"]), nonce)
        if meta.get("userinfo_endpoint") and tokens.get("access_token"):
            info = client.get(
                meta["userinfo_endpoint"], headers={"Authorization": f"Bearer {tokens['access_token']}"}
            )
            if info.status_code < 400:
                extra = info.json()
                # Userinfo must describe the same subject; never let it change sub/iss/aud.
                if isinstance(extra, dict) and extra.get("sub") == claims["sub"]:
                    claims = {**extra, **claims}  # the verified ID token wins on conflicts
    groups = claims.get(provider.groups_claim) or []
    if isinstance(groups, str):
        groups = [groups]
    email = str(claims.get("email") or "").lower()
    verified = claims.get("email_verified")
    return OIDCIdentity(
        provider=provider.id,
        subject=str(claims["sub"]),
        email=email,
        email_verified=bool(email) and (verified is True or (verified is None and provider.trust_email)),
        name=str(claims.get("name") or ""),
        username=str(claims.get("preferred_username") or claims.get("nickname") or email),
        groups=[str(g) for g in groups][:500],
    )


def state_payload(provider: OIDCProvider, **extra: Any) -> tuple[dict[str, Any], str]:
    """Session data for one login attempt and the authorize URL to redirect to."""
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    url = authorize_url(provider, state=state, nonce=nonce, code_challenge=challenge)
    return {"provider": provider.id, "state": state, "nonce": nonce, "verifier": verifier, **extra}, url
