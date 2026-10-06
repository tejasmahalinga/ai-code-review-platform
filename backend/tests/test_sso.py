"""ADM-07 single sign-on: OpenID Connect, role mapping, password policy, linked identities."""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlparse

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from rest_framework.test import APIClient

from apps.accounts import oidc
from apps.accounts.models import ExternalIdentity, Invite, User
from apps.audit.models import AuditEvent
from tests.factories import cached_pem
from tests.http import MockRouter

ISSUER = "https://idp.example.com"
CLIENT_ID = "reviewbot-client"


@pytest.fixture
def idp(monkeypatch, settings):
    """A fake OpenID provider with a real RS256 key. ``state`` controls the next sign-in's claims."""
    settings.OIDC_ISSUER = ISSUER
    settings.OIDC_CLIENT_ID = CLIENT_ID
    settings.OIDC_CLIENT_SECRET = "client-secret"
    settings.OIDC_NAME = "Okta"
    settings.OIDC_ROLE_MAPPING = ""
    private_key = serialization.load_pem_private_key(cached_pem().encode(), password=None)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update(kid="key-1", use="sig", alg="RS256")
    state = {
        "claims": {"sub": "okta|123", "email": "ada@corp.example", "email_verified": True, "name": "Ada"},
        "groups": [],
        "nonce": None,
        "token_requests": [],
        "kid": "key-1",
        "issuer": ISSUER,
        "audience": CLIENT_ID,
        "alg": "RS256",
    }
    router = MockRouter()
    router.add(
        "GET",
        r"/\.well-known/openid-configuration$",
        {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "userinfo_endpoint": f"{ISSUER}/userinfo",
            "jwks_uri": f"{ISSUER}/keys",
        },
    )
    router.add("GET", r"/keys$", {"keys": [public_jwk]})

    def token(request: httpx2.Request) -> httpx2.Response:
        form = parse_qs(request.content.decode())
        state["token_requests"].append({"form": form, "auth": request.headers.get("authorization", "")})
        now = int(time.time())
        claims = {
            "iss": state["issuer"],
            "aud": state["audience"],
            "iat": now,
            "exp": now + 300,
            "nonce": state["nonce"],
            **state["claims"],
        }
        if state["alg"] == "HS256":
            id_token = jwt.encode(claims, "client-secret-long-enough-for-hs256!!", algorithm="HS256")
        else:
            id_token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": state["kid"]})
        return httpx2.Response(
            200, json={"id_token": id_token, "access_token": "at-123", "token_type": "Bearer"}
        )

    router.add("POST", r"/token$", handler=token)
    router.add(
        "GET",
        r"/userinfo$",
        handler=lambda _r: httpx2.Response(
            200, json={"sub": state["claims"]["sub"], "groups": state["groups"]}
        ),
    )
    monkeypatch.setattr(oidc, "http_client", router.client)
    state["router"] = router
    return state


def sso_login(client: APIClient, idp: dict, query: str = "", provider: str = "sso") -> str:
    start = client.get(f"/api/v1/auth/oidc/{provider}/start{query}")
    assert start.status_code == 302, start.content
    params = parse_qs(urlparse(start["Location"]).query)
    assert params["code_challenge_method"] == ["S256"] and params["client_id"] == [CLIENT_ID]
    idp["nonce"] = params["nonce"][0]
    done = client.get(f"/api/v1/auth/oidc/{provider}/callback", {"code": "c0de", "state": params["state"][0]})
    assert done.status_code == 302
    location = urlparse(done["Location"])
    return location.path + (f"?{location.query}" if location.query else "")


# --- configuration -----------------------------------------------------------------------------


def test_role_mapping_parse_and_priority():
    mapping = oidc.parse_role_mapping("viewer=*; admin=platform ;reviewer=eng,qa")
    provider = oidc.OIDCProvider("sso", "x", ISSUER, "c", "s", role_mapping=mapping)
    assert provider.role_for(["eng", "platform"]) == "admin"
    assert provider.role_for(["qa"]) == "reviewer"
    assert provider.role_for([]) == "viewer"
    with pytest.raises(ValueError):
        oidc.parse_role_mapping("owner=x")


@pytest.mark.django_db
def test_providers_listed_on_login_page(anon, idp, settings):
    settings.GITLAB_OAUTH_CLIENT_ID = "gl-id"
    settings.GITLAB_OAUTH_CLIENT_SECRET = "gl-secret"
    settings.GITLAB_OAUTH_URL = "https://gitlab.example"
    data = anon.get("/api/v1/setup/status").json()
    assert [(p["id"], p["name"]) for p in data["login_providers"]] == [("sso", "Okta"), ("gitlab", "GitLab")]
    assert oidc.get_provider("gitlab").issuer == "https://gitlab.example"


# --- sign-in -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_existing_account_is_linked_by_verified_email(anon, idp, admin_user):
    idp["claims"]["email"] = "ADMIN@example.com"
    assert sso_login(anon, idp, "?next=/reviews/4") == "/reviews/4"
    assert anon.get("/api/v1/auth/me").json()["email"] == "admin@example.com"
    identity = ExternalIdentity.objects.get()
    assert (identity.user, identity.provider, identity.subject) == (admin_user, "sso", "okta|123")
    # PKCE verifier and client authentication were sent to the token endpoint.
    request = idp["token_requests"][0]
    assert request["form"]["code_verifier"][0] and request["auth"].startswith("Basic ")
    # The next sign-in uses the subject, even if the email changed at the IdP.
    idp["claims"]["email"] = "renamed@example.com"
    assert sso_login(APIClient(), idp) == "/pull-requests"
    assert {"auth.sso_linked", "auth.login"} <= set(AuditEvent.objects.values_list("action", flat=True))


@pytest.mark.django_db
def test_unknown_user_without_mapping_is_refused(anon, idp, admin_user):
    assert sso_login(anon, idp) == "/login?error=no_account"
    idp["claims"]["email_verified"] = False
    idp["claims"]["email"] = "admin@example.com"
    assert sso_login(anon, idp) == "/login?error=no_account"  # unverified emails never link accounts
    assert not ExternalIdentity.objects.exists()


@pytest.mark.django_db
def test_trust_email_for_idps_without_email_verified(anon, idp, admin_user, settings):
    del idp["claims"]["email_verified"]
    idp["claims"]["email"] = "admin@example.com"
    assert sso_login(anon, idp) == "/login?error=no_account"
    settings.OIDC_TRUST_EMAIL = True
    assert sso_login(anon, idp) == "/pull-requests"


@pytest.mark.django_db
def test_group_mapping_provisions_and_syncs_roles(anon, idp, settings, admin_user):
    settings.OIDC_ROLE_MAPPING = "admin=platform;reviewer=engineering"
    idp["groups"] = ["marketing"]
    assert sso_login(anon, idp) == "/login?error=not_in_allowed_group"
    idp["groups"] = ["engineering"]
    assert sso_login(anon, idp) == "/pull-requests"
    user = User.objects.get(email="ada@corp.example")
    assert user.role == "reviewer" and not user.has_usable_password()
    idp["groups"] = ["engineering", "platform"]
    sso_login(APIClient(), idp)
    user.refresh_from_db()
    assert user.role == "admin"
    assert AuditEvent.objects.filter(action="user.role_synced").exists()
    idp["groups"] = []
    assert sso_login(APIClient(), idp) == "/login?error=not_in_allowed_group"  # removed from the IdP groups


@pytest.mark.django_db
def test_role_sync_never_demotes_the_last_admin(anon, idp, settings, admin_user):
    settings.OIDC_ROLE_MAPPING = "admin=platform;viewer=*"
    idp["claims"]["email"] = "admin@example.com"
    sso_login(anon, idp)
    admin_user.refresh_from_db()
    assert admin_user.role == "admin"


@pytest.mark.django_db
def test_invite_accepted_through_sso(anon, idp, api):
    created = api.post(
        "/api/v1/invites", {"email": "new@corp.example", "role": "reviewer"}, format="json"
    ).json()
    token = created["url"].rsplit("/", 1)[-1]
    assert sso_login(anon, idp, f"?invite={token}") == "/pull-requests"
    user = User.objects.get(email="new@corp.example")
    assert user.role == "reviewer" and user.identities.get().subject == "okta|123"
    assert Invite.objects.get().status == "accepted"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("tamper", "value"),
    [
        ("issuer", "https://evil.example"),
        ("audience", "someone-else"),
        ("kid", "unknown-key"),
        ("alg", "HS256"),
    ],
)
def test_invalid_id_tokens_are_rejected(anon, idp, admin_user, tamper, value):
    idp["claims"]["email"] = "admin@example.com"
    idp[tamper] = value
    assert sso_login(anon, idp) == "/login?error=sso_failed"
    assert anon.get("/api/v1/auth/me").status_code == 401


@pytest.mark.django_db
def test_nonce_and_state_are_enforced(anon, idp, admin_user):
    idp["claims"]["email"] = "admin@example.com"
    start = anon.get("/api/v1/auth/oidc/sso/start")
    params = parse_qs(urlparse(start["Location"]).query)
    idp["nonce"] = "replayed-nonce"
    response = anon.get("/api/v1/auth/oidc/sso/callback", {"code": "c", "state": params["state"][0]})
    assert response["Location"].endswith("/login?error=sso_failed")
    forged = anon.get("/api/v1/auth/oidc/sso/callback", {"code": "c", "state": "forged"})
    assert forged["Location"].endswith("/login?error=sso_state")
    assert anon.get("/api/v1/auth/oidc/nope/start")["Location"].endswith("/login?error=sso_unavailable")


@pytest.mark.django_db
def test_disabled_accounts_cannot_sign_in(anon, idp, admin_user):
    other = User.objects.create_user("ada@corp.example", "correct-horse-battery", is_active=False)
    assert sso_login(anon, idp) == "/login?error=account_disabled"
    assert other.identities.count() == 0


# --- linking and identities ----------------------------------------------------------------------


@pytest.mark.django_db
def test_link_list_and_unlink(api, idp, admin_user):
    assert sso_login(api, idp, "?link=1") == "/settings/account?sso=linked"
    rows = api.get("/api/v1/auth/me/identities").json()
    assert [(r["provider"], r["provider_name"]) for r in rows] == [("sso", "Okta")]
    assert api.delete(f"/api/v1/auth/me/identities/{rows[0]['id']}").status_code == 204
    assert not ExternalIdentity.objects.exists()


@pytest.mark.django_db
def test_cannot_unlink_the_only_sign_in_method(idp, settings):
    settings.OIDC_ROLE_MAPPING = "viewer=*"
    client = APIClient()
    sso_login(client, idp)
    identity = ExternalIdentity.objects.get()
    assert client.delete(f"/api/v1/auth/me/identities/{identity.pk}").status_code == 400


# --- password policy -----------------------------------------------------------------------------


@pytest.mark.django_db
def test_password_login_policy(anon, settings, admin_user):
    User.objects.create_user("rev@example.com", "correct-horse-battery", role="reviewer")
    creds = {"email": "rev@example.com", "password": "correct-horse-battery"}
    settings.PASSWORD_LOGIN = "admins"
    response = anon.post("/api/v1/auth/login", creds, format="json")
    assert response.status_code == 403 and response.json()["error"]["code"] == "password_login_disabled"
    admin = {"email": "admin@example.com", "password": "correct-horse-battery"}
    assert anon.post("/api/v1/auth/login", admin, format="json").status_code == 200  # break-glass for admins
    settings.PASSWORD_LOGIN = "none"
    assert APIClient().post("/api/v1/auth/login", admin, format="json").status_code == 403


@pytest.mark.django_db
def test_invites_need_sso_when_passwords_are_off(api, anon, settings):
    created = api.post("/api/v1/invites", {"email": "x@corp.example", "role": "viewer"}, format="json").json()
    token = created["url"].rsplit("/", 1)[-1]
    settings.PASSWORD_LOGIN = "none"
    response = anon.post(f"/api/v1/auth/invites/{token}", {"password": "a-strong-passphrase"}, format="json")
    assert response.status_code == 403
    assert anon.get(f"/api/v1/auth/invites/{token}").json()["password_login"] == "none"
