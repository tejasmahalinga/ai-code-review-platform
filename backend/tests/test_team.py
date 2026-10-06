"""ADM-02 roles & invites, ADM-03 sign in with GitHub, ADM-04 audit log."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from django.core import mail
from django.db import DatabaseError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import github_oauth
from apps.accounts.models import Invite, User, hash_token
from apps.audit.models import AuditEvent
from apps.audit.tasks import prune_audit_events
from apps.reviews.models import Finding, ReviewRun
from tests.factories import make_connection, make_pull_request, make_repository
from tests.http import MockRouter


def client_for(user: User) -> APIClient:
    client = APIClient()
    client.force_login(user)
    return client


@pytest.fixture
def reviewer(db) -> User:
    return User.objects.create_user("rev@example.com", "correct-horse-battery", role="reviewer")


@pytest.fixture
def viewer(db) -> User:
    return User.objects.create_user("view@example.com", "correct-horse-battery", role="viewer")


@pytest.fixture
def world(fake_credential):
    repo = make_repository(credential=fake_credential, name="app")
    pr = make_pull_request(repo, 1)
    run = ReviewRun.objects.create(pull_request=pr, trigger="manual", head_sha="x", status="completed")
    finding = Finding.objects.create(
        review_run=run, fingerprint="f", path="a.py", category="bug", severity="high",
        title="Bug", body="b", post_status="posted",
    )  # fmt: skip
    return {"repo": repo, "pr": pr, "run": run, "finding": finding, "credential": fake_credential}


def actions() -> list[str]:
    return list(AuditEvent.objects.order_by("id").values_list("action", flat=True))


# --- ADM-02 permission matrix ------------------------------------------------------------------

# (method, path template, body, roles allowed)
MATRIX = [
    ("get", "/api/v1/pull-requests", None, {"admin", "reviewer", "viewer"}),
    ("get", "/api/v1/reviews/{run}", None, {"admin", "reviewer", "viewer"}),
    ("get", "/api/v1/repositories/{repo}/settings", None, {"admin", "reviewer", "viewer"}),
    ("get", "/api/v1/feedback-stats", None, {"admin", "reviewer", "viewer"}),
    ("post", "/api/v1/pull-requests/{pr}/reviews", None, {"admin", "reviewer"}),
    ("patch", "/api/v1/findings/{finding}", {"state": "accepted"}, {"admin", "reviewer"}),
    ("put", "/api/v1/findings/{finding}/feedback", {"vote": "up"}, {"admin", "reviewer"}),
    ("patch", "/api/v1/repositories/{repo}/settings", {"custom_instructions": "x"}, {"admin", "reviewer"}),
    ("patch", "/api/v1/repositories/{repo}/settings", {"max_files": 10}, {"admin"}),
    ("patch", "/api/v1/repositories/{repo}", {"enabled": False}, {"admin"}),
    ("get", "/api/v1/llm-credentials", None, {"admin"}),
    ("get", "/api/v1/usage", None, {"admin"}),
    ("get", "/api/v1/webhook-deliveries", None, {"admin"}),
    ("get", "/api/v1/integrations/github", None, {"admin"}),
    ("get", "/api/v1/users", None, {"admin"}),
    ("get", "/api/v1/invites", None, {"admin"}),
    ("get", "/api/v1/audit-events", None, {"admin"}),
]


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["admin", "reviewer", "viewer"])
@pytest.mark.parametrize(("method", "path", "body", "allowed"), MATRIX)
def test_permission_matrix(
    role, method, path, body, allowed, world, admin_user, reviewer, viewer, monkeypatch
):
    monkeypatch.setattr("apps.reviews.services.enqueue", lambda *a, **k: None)
    user = {"admin": admin_user, "reviewer": reviewer, "viewer": viewer}[role]
    url = path.format(
        run=world["run"].pk, repo=world["repo"].pk, pr=world["pr"].pk, finding=world["finding"].pk
    )
    response = getattr(client_for(user), method)(url, body, format="json")
    if role in allowed:
        assert response.status_code < 400, response.content
    else:
        assert response.status_code == 403, response.content


@pytest.mark.django_db
def test_reviewer_is_told_which_settings_are_admin_only(reviewer, world):
    response = client_for(reviewer).patch(
        f"/api/v1/repositories/{world['repo'].pk}/settings",
        {"rules": [], "credential": None, "auto_review": False},
        format="json",
    )
    assert response.status_code == 403
    assert "auto_review, credential" in response.json()["error"]["message"]


@pytest.mark.django_db
def test_new_users_default_to_viewer():
    assert User.objects.create_user("x@example.com", "pw-long-enough-1").role == "viewer"


# --- ADM-02 user management --------------------------------------------------------------------


@pytest.mark.django_db
def test_admin_changes_role_and_deactivates(api, viewer):
    response = api.patch(f"/api/v1/users/{viewer.pk}", {"role": "reviewer"}, format="json")
    assert response.json()["role"] == "reviewer"
    viewer_client = client_for(viewer)
    assert api.patch(f"/api/v1/users/{viewer.pk}", {"is_active": False}, format="json").status_code == 200
    assert viewer_client.get("/api/v1/auth/me").status_code == 401  # existing sessions stop working
    assert actions()[-2:] == ["user.role_changed", "user.deactivated"]
    event = AuditEvent.objects.get(action="user.role_changed")
    assert event.metadata == {"from": "viewer", "to": "reviewer"}


@pytest.mark.django_db
def test_last_admin_cannot_be_removed(api, admin_user):
    response = api.patch(f"/api/v1/users/{admin_user.pk}", {"role": "viewer"}, format="json")
    assert response.status_code == 400
    assert api.patch(f"/api/v1/users/{admin_user.pk}", {"is_active": False}, format="json").status_code == 400
    other = User.objects.create_superuser("admin2@example.com", "correct-horse-battery")
    assert api.patch(f"/api/v1/users/{other.pk}", {"role": "viewer"}, format="json").status_code == 200


@pytest.mark.django_db
def test_me_update_and_password_change(api, admin_user):
    assert (
        api.patch("/api/v1/auth/me", {"name": "Ada", "role": "viewer"}, format="json").json()["role"]
        == "admin"
    )
    bad = api.post(
        "/api/v1/auth/password",
        {"current_password": "nope", "new_password": "new-horse-battery-9"},
        format="json",
    )
    assert bad.status_code == 400
    ok = api.post(
        "/api/v1/auth/password",
        {"current_password": "correct-horse-battery", "new_password": "new-horse-battery-9"},
        format="json",
    )
    assert ok.status_code == 204
    admin_user.refresh_from_db()
    assert admin_user.check_password("new-horse-battery-9") and admin_user.name == "Ada"
    assert api.get("/api/v1/auth/me").status_code == 200  # this session survives the change


# --- ADM-02 invites ----------------------------------------------------------------------------


def invite(api, email="new@example.com", role="reviewer") -> dict:
    response = api.post("/api/v1/invites", {"email": email, "role": role}, format="json")
    assert response.status_code == 201, response.content
    return response.json()


def token_of(data: dict) -> str:
    return data["url"].rsplit("/", 1)[-1]


@pytest.mark.django_db
def test_invite_flow(api, anon):
    data = invite(api, "New@Example.com")
    token = token_of(data)
    assert data["email"] == "new@example.com" and data["status"] == "pending" and data["email_sent"] is True
    assert Invite.objects.get().token_hash == hash_token(token)  # only the hash is stored
    assert token in mail.outbox[0].body
    assert "url" not in api.get("/api/v1/invites").json()[0]

    info = anon.get(f"/api/v1/auth/invites/{token}").json()
    assert info["email"] == "new@example.com" and info["role"] == "reviewer"
    weak = anon.post(f"/api/v1/auth/invites/{token}", {"password": "123"}, format="json")
    assert weak.status_code == 400
    response = anon.post(
        f"/api/v1/auth/invites/{token}", {"name": "New", "password": "a-strong-passphrase"}, format="json"
    )
    assert response.status_code == 201
    assert response.json()["role"] == "reviewer"
    assert anon.get("/api/v1/auth/me").json()["email"] == "new@example.com"  # signed in
    assert APIClient().get(f"/api/v1/auth/invites/{token}").status_code == 404  # single use
    assert {"user.invited", "invite.accepted", "auth.login"} <= set(actions())


@pytest.mark.django_db
def test_invite_rules(api, anon, viewer):
    assert (
        api.post("/api/v1/invites", {"email": viewer.email, "role": "viewer"}, format="json").status_code
        == 400
    )
    first = invite(api)
    second = invite(api)  # re-inviting replaces the pending invite
    assert anon.get(f"/api/v1/auth/invites/{token_of(first)}").status_code == 404
    assert anon.get(f"/api/v1/auth/invites/{token_of(second)}").status_code == 200
    assert api.delete(f"/api/v1/invites/{second['id']}").status_code == 204
    assert anon.get(f"/api/v1/auth/invites/{token_of(second)}").status_code == 404
    expired = invite(api, "late@example.com")
    Invite.objects.filter(pk=expired["id"]).update(expires_at=timezone.now() - timedelta(minutes=1))
    assert anon.get(f"/api/v1/auth/invites/{token_of(expired)}").status_code == 404
    assert anon.get("/api/v1/auth/invites/not-a-token").status_code == 404


# --- ADM-03 sign in with GitHub ----------------------------------------------------------------


@pytest.fixture
def github(db, monkeypatch, settings):
    make_connection()
    settings.GITHUB_OAUTH_CLIENT_ID = "cid"
    settings.GITHUB_OAUTH_CLIENT_SECRET = "csecret"
    router = MockRouter()
    state = {"user": {"id": 4242, "login": "octo", "name": "Octo Cat"}, "emails": []}
    router.add("POST", r"/login/oauth/access_token$", {"access_token": "ghu_abcdefghijklmnopqrstuvwxyz"})
    router.add("GET", r"/user$", handler=lambda _r: httpx2.Response(200, json=state["user"]))
    router.add("GET", r"/user/emails$", handler=lambda _r: httpx2.Response(200, json=state["emails"]))
    monkeypatch.setattr(github_oauth, "http_client", router.client)
    state["router"] = router
    return state


def oauth(client: APIClient, query: str = "") -> str:
    """Runs start → (GitHub) → callback and returns the final redirect path."""
    start = client.get(f"/api/v1/auth/github/start{query}")
    assert start.status_code == 302
    params = parse_qs(urlparse(start["Location"]).query)
    assert params["client_id"] == ["cid"]
    done = client.get("/api/v1/auth/github/callback", {"code": "c0de", "state": params["state"][0]})
    assert done.status_code == 302
    location = urlparse(done["Location"])
    return location.path + (f"?{location.query}" if location.query else "")


@pytest.mark.django_db
def test_github_login_needs_an_account(github, anon, admin_user):
    github["emails"] = [{"email": "stranger@example.com", "verified": True, "primary": True}]
    assert oauth(anon) == "/login?error=no_account"
    assert anon.get("/api/v1/auth/me").status_code == 401
    assert AuditEvent.objects.filter(action="auth.login_failed").exists()
    # The token never reaches the audit log.
    assert "ghu_" not in str(list(AuditEvent.objects.values_list("metadata", flat=True)))


@pytest.mark.django_db
def test_github_login_links_by_verified_email_only(github, anon, admin_user):
    github["emails"] = [{"email": "admin@example.com", "verified": False, "primary": True}]
    assert oauth(anon) == "/login?error=no_account"
    github["emails"] = [
        {"email": "other@example.com", "verified": True, "primary": False},
        {"email": "ADMIN@example.com", "verified": True, "primary": True},
    ]
    assert oauth(anon, "?next=/reviews/1") == "/reviews/1"
    admin_user.refresh_from_db()
    assert (admin_user.github_id, admin_user.github_login) == (4242, "octo")
    # Next time the GitHub id alone is enough, even if the email changed on GitHub.
    github["emails"] = []
    assert oauth(APIClient()) == "/pull-requests"


@pytest.mark.django_db
def test_github_accepts_invite(github, api, anon):
    token = token_of(invite(api, "invitee@corp.example", "reviewer"))
    assert oauth(anon, f"?invite={token}") == "/pull-requests"
    user = User.objects.get(email="invitee@corp.example")
    assert (user.role, user.github_id, user.name, user.has_usable_password()) == (
        "reviewer",
        4242,
        "Octo Cat",
        False,
    )
    assert Invite.objects.get().status == "accepted"


@pytest.mark.django_db
def test_github_signup_respects_domains(github, anon, settings, admin_user):
    settings.ALLOW_SIGNUP = True
    settings.SIGNUP_EMAIL_DOMAINS = ["corp.example"]
    settings.SIGNUP_ROLE = "admin"  # never honoured for self-signup
    github["emails"] = [{"email": "dev@gmail.com", "verified": True, "primary": True}]
    assert oauth(anon) == "/login?error=no_account"
    github["emails"] = [{"email": "dev@corp.example", "verified": True, "primary": True}]
    assert oauth(anon) == "/pull-requests"
    assert User.objects.get(email="dev@corp.example").role == "viewer"


@pytest.mark.django_db
def test_github_rejects_forged_state_and_disabled_accounts(github, anon, admin_user):
    anon.get("/api/v1/auth/github/start")
    forged = anon.get("/api/v1/auth/github/callback", {"code": "c", "state": "forged"})
    assert forged["Location"].endswith("/login?error=github_state")
    User.objects.filter(pk=admin_user.pk).update(github_id=4242, is_active=False)
    assert oauth(anon) == "/login?error=account_disabled"


@pytest.mark.django_db
def test_link_and_unlink_github(github, admin_user, viewer):
    client = client_for(admin_user)
    assert oauth(client, "?link=1") == "/settings/account?github=linked"
    admin_user.refresh_from_db()
    assert admin_user.github_id == 4242
    assert oauth(client_for(viewer), "?link=1") == "/settings/account?github=in_use"
    assert client.delete("/api/v1/auth/me/github").json()["github_login"] == ""
    assert {"auth.github_linked", "auth.github_unlinked"} <= set(actions())


@pytest.mark.django_db
def test_github_login_unavailable_without_client(anon, settings):
    settings.GITHUB_OAUTH_CLIENT_ID = ""
    assert anon.get("/api/v1/setup/status").json()["github_login"] is False
    assert anon.get("/api/v1/auth/github/start")["Location"].endswith("/login?error=github_unavailable")


@pytest.mark.django_db
def test_github_app_client_credentials_are_used(settings):
    connection = make_connection()
    connection.client_id = "Iv1.app"
    connection.set_secrets(private_key=connection.private_key, webhook_secret="w", client_secret="app-secret")
    connection.save()
    assert github_oauth.oauth_client() == github_oauth.OAuthClient("Iv1.app", "app-secret")
    settings.GITHUB_LOGIN_ENABLED = False
    assert github_oauth.oauth_client() is None


# --- ADM-04 audit log --------------------------------------------------------------------------


@pytest.mark.django_db
def test_auth_events(anon, admin_user):
    anon.post("/api/v1/auth/login", {"email": "admin@example.com", "password": "wrong"}, format="json")
    anon.post(
        "/api/v1/auth/login",
        {"email": "admin@example.com", "password": "correct-horse-battery"},
        format="json",
    )
    anon.post("/api/v1/auth/logout")
    assert actions() == ["auth.login_failed", "auth.login", "auth.logout"]
    failed = AuditEvent.objects.get(action="auth.login_failed")
    assert (
        failed.actor is None
        and failed.metadata == {"email": "admin@example.com"}
        and failed.ip == "127.0.0.1"
    )


@pytest.mark.django_db
def test_credential_events_never_contain_secrets(api):
    created = api.post(
        "/api/v1/llm-credentials",
        {"name": "Demo", "provider": "fake", "default_model": "demo", "api_key": "sk-live-1234567890abcdef"},
        format="json",
    ).json()
    response = api.post(
        f"/api/v1/llm-credentials/{created['id']}/rotate",
        {"api_key": "sk-live-abcdef1234567890"},
        format="json",
    )
    assert response.status_code == 200 and response.json()["last4"] == "7890"
    api.patch(f"/api/v1/llm-credentials/{created['id']}", {"name": "Renamed"}, format="json")
    api.delete(f"/api/v1/llm-credentials/{created['id']}")
    assert actions() == [
        "credential.created",
        "credential.rotated",
        "credential.updated",
        "credential.revoked",
    ]
    assert AuditEvent.objects.filter(action="credential.rotated").count() == 1
    rotated = AuditEvent.objects.get(action="credential.rotated")
    assert (rotated.actor_email, rotated.target_type, rotated.target_id) == (
        "admin@example.com",
        "llmcredential",
        str(created["id"]),
    )
    assert AuditEvent.objects.get(action="credential.updated").metadata["changes"]["name"] == {
        "from": "Demo",
        "to": "Renamed",
    }
    assert "sk-live" not in str(list(AuditEvent.objects.values_list("metadata", "target_label")))


@pytest.mark.django_db
def test_settings_change_records_diff(api, world):
    api.patch(
        f"/api/v1/repositories/{world['repo'].pk}/settings",
        {"min_severity": "high", "custom_instructions": "Be brief"},
        format="json",
    )
    api.patch(f"/api/v1/repositories/{world['repo'].pk}/settings", {"min_severity": "high"}, format="json")
    event = AuditEvent.objects.get(action="repository.settings_changed")  # the no-op save adds nothing
    assert event.target_label == "acme/app"
    assert event.metadata["changes"] == {
        "custom_instructions": {"from": "", "to": "Be brief"},
        "min_severity": {"from": "low", "to": "high"},
    }
    api.patch(f"/api/v1/repositories/{world['repo'].pk}", {"enabled": False}, format="json")
    assert actions()[-1] == "repository.disabled"


@pytest.mark.django_db
def test_audit_log_is_append_only(api, admin_user):
    api.post("/api/v1/auth/logout")
    event = AuditEvent.objects.get()
    with pytest.raises(RuntimeError):
        event.save()
    with pytest.raises(DatabaseError), transaction.atomic():
        AuditEvent.objects.filter(pk=event.pk).update(action="tampered")
    admin = client_for(admin_user)
    assert admin.post("/api/v1/audit-events", {}, format="json").status_code == 405
    assert admin.delete(f"/api/v1/audit-events/{event.pk}").status_code in (404, 405)
    # Deleting the actor keeps the event (actor nulled, email snapshot kept).
    User.objects.create_superuser("keep@example.com", "correct-horse-battery")
    admin_user.delete()
    event = AuditEvent.objects.get()
    assert event.actor_id is None and event.actor_email == "admin@example.com"


@pytest.mark.django_db
def test_audit_api_filters(api, admin_user):
    api.post("/api/v1/invites", {"email": "a@example.com", "role": "viewer"}, format="json")
    api.patch("/api/v1/auth/me", {"name": "x"}, format="json")
    api.post("/api/v1/auth/logout")
    api.force_login(admin_user)
    rows = api.get("/api/v1/audit-events", {"action": "user"}).json()["results"]
    assert [r["action"] for r in rows] == ["user.invited"]
    assert rows[0]["target_label"] == "a@example.com"
    assert len(api.get("/api/v1/audit-events", {"action": "auth.logout"}).json()["results"]) == 1
    assert api.get("/api/v1/audit-events", {"since": "yesterday"}).status_code == 400
    future = (timezone.now() + timedelta(days=1)).isoformat()
    assert api.get("/api/v1/audit-events", {"since": future}).json()["results"] == []


@pytest.mark.django_db
def test_audit_retention(settings, admin_user):
    settings.AUDIT_RETENTION_DAYS = 30
    AuditEvent.objects.create(action="old.event", created_at=timezone.now() - timedelta(days=31))
    AuditEvent.objects.create(action="new.event")
    assert prune_audit_events() == 1
    assert actions() == ["new.event"]


@pytest.mark.django_db
def test_review_and_finding_events(world, reviewer, monkeypatch):
    monkeypatch.setattr("apps.reviews.services.enqueue", lambda *a, **k: None)
    client = client_for(reviewer)
    client.post(f"/api/v1/pull-requests/{world['pr'].pk}/reviews")
    client.patch(
        f"/api/v1/findings/{world['finding'].pk}",
        {"state": "dismissed", "dismiss_reason": "false_positive"},
        format="json",
    )
    assert actions() == ["review.requested", "finding.state_changed"]
    assert AuditEvent.objects.get(action="finding.state_changed").actor_email == "rev@example.com"
