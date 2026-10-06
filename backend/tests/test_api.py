from __future__ import annotations

import pytest

from apps.repositories.models import GitProviderConnection
from apps.reviews.models import Finding, LLMUsage, ReviewRun
from tests.factories import EnqueueRecorder, make_connection, make_pull_request, make_repository
from tests.http import MockRouter

pytestmark = pytest.mark.django_db


@pytest.fixture
def no_enqueue(monkeypatch):
    calls = EnqueueRecorder()
    monkeypatch.setattr("apps.reviews.services.enqueue", calls)
    return calls


# --- repositories -------------------------------------------------------------------------------


def test_repository_list_and_settings_round_trip(api, fake_credential):
    repo = make_repository(credential=fake_credential, enabled=False)
    listing = api.get("/api/v1/repositories").json()
    assert listing[0]["full_name"] == repo.full_name
    assert listing[0]["credential_name"] == "Demo"

    patch = {
        "auto_review": False,
        "ignore_patterns": ["docs/", "*.md", "  "],
        "custom_instructions": "Flag raw SQL.",
        "min_severity": "medium",
        "max_inline_comments": 10,
        "max_changed_lines": 500,
        "review_drafts": True,
    }
    response = api.patch(f"/api/v1/repositories/{repo.pk}/settings", patch, format="json")
    assert response.status_code == 200, response.json()
    data = api.get(f"/api/v1/repositories/{repo.pk}/settings").json()
    assert data["ignore_patterns"] == ["docs/", "*.md"]
    for key in (
        "auto_review",
        "custom_instructions",
        "min_severity",
        "max_inline_comments",
        "max_changed_lines",
        "review_drafts",
    ):
        assert data[key] == patch[key]
    assert "package-lock.json" in data["default_ignore_patterns"]
    assert data["effective_model"] == "demo"


def test_custom_instructions_length_limit(api, fake_credential):
    repo = make_repository(credential=fake_credential)
    response = api.patch(
        f"/api/v1/repositories/{repo.pk}/settings", {"custom_instructions": "x" * 4001}, format="json"
    )
    assert response.status_code == 400


def test_enable_requires_valid_credential(api, fake_credential):
    repo = make_repository(credential=None, enabled=False)
    response = api.patch(f"/api/v1/repositories/{repo.pk}", {"enabled": True}, format="json")
    assert response.status_code == 400
    assert "LLM key" in response.json()["error"]["message"]
    api.patch(f"/api/v1/repositories/{repo.pk}/settings", {"credential": fake_credential.pk}, format="json")
    assert (
        api.patch(f"/api/v1/repositories/{repo.pk}", {"enabled": True}, format="json").json()["enabled"]
        is True
    )


def test_revoked_credential_cannot_be_selected(api, fake_credential):
    repo = make_repository(credential=None, enabled=False)
    fake_credential.revoke()
    response = api.patch(
        f"/api/v1/repositories/{repo.pk}/settings", {"credential": fake_credential.pk}, format="json"
    )
    assert response.status_code == 400


def test_enabled_repo_cannot_drop_its_credential(api, fake_credential):
    repo = make_repository(credential=fake_credential)
    response = api.patch(f"/api/v1/repositories/{repo.pk}/settings", {"credential": None}, format="json")
    assert response.status_code == 400


def test_unusual_ignore_pattern_never_errors(api, fake_credential):
    repo = make_repository(credential=fake_credential)
    response = api.patch(
        f"/api/v1/repositories/{repo.pk}/settings", {"ignore_patterns": ["a/**/b/***x["]}, format="json"
    )
    assert response.status_code in (200, 400)  # pathspec accepts most input; must never 500


# --- GitHub integration -----------------------------------------------------------------------


def test_github_status_when_not_connected(api):
    data = api.get("/api/v1/integrations/github").json()
    assert data["connected"] is False
    assert data["webhook_url"].endswith("/webhooks/github")


def test_manifest_flow(api, monkeypatch, settings):
    start = api.post(
        "/api/v1/integrations/github/manifest",
        {"app_name": "Acme Reviewbot", "organization": "acme"},
        format="json",
    ).json()
    assert (
        start["post_url"] == f"https://github.com/organizations/acme/settings/apps/new?state={start['state']}"
    )
    assert '"name": "Acme Reviewbot"' in start["manifest"]

    router = MockRouter()
    router.add(
        "POST",
        r"/app-manifests/abc/conversions$",
        {
            "id": 99,
            "slug": "acme-reviewbot",
            "name": "Acme Reviewbot",
            "html_url": "https://github.com/apps/acme-reviewbot",
            "client_id": "Iv1.x",
            "client_secret": "cs",
            "webhook_secret": "whs",
            "pem": "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
        },
    )
    from apps.git_providers.github import client as gh_client

    original = gh_client.exchange_manifest_code
    monkeypatch.setattr(
        "apps.repositories.views.exchange_manifest_code",
        lambda api_url, code: original(api_url, code, router.client()),
    )
    bad = api.get("/api/v1/integrations/github/callback", {"code": "abc", "state": "forged"})
    assert bad.status_code == 302 and bad["Location"].endswith("github=invalid_state")

    api.post("/api/v1/integrations/github/manifest", {}, format="json")
    state = api.session["github_manifest_state"]
    ok = api.get("/api/v1/integrations/github/callback", {"code": "abc", "state": state})
    assert (
        ok.status_code == 302
        and ok["Location"] == f"{settings.PUBLIC_URL}/settings/integrations?github=connected"
    )
    connection = GitProviderConnection.objects.get()
    assert connection.app_id == "99"
    assert connection.webhook_secret == "whs"
    assert "BEGIN RSA" not in connection.encrypted_private_key
    status = api.get("/api/v1/integrations/github").json()
    assert (
        status["connected"]
        and status["install_url"] == "https://github.com/apps/acme-reviewbot/installations/new"
    )
    assert api.post("/api/v1/integrations/github/manifest", {}, format="json").status_code == 409


def test_callback_redirects_anonymous_to_login(anon, settings):
    response = anon.get("/api/v1/integrations/github/callback", {"code": "x", "state": "y"})
    assert response.status_code == 302 and response["Location"] == f"{settings.PUBLIC_URL}/login"


def test_sync_endpoint(api, monkeypatch):
    make_connection()
    router = MockRouter()
    router.add(
        "GET", r"/app/installations\?", [{"id": 77, "account": {"login": "acme", "type": "Organization"}}]
    )
    router.add("POST", r"/app/installations/77/access_tokens$", {"token": "ghs_x"})
    router.add(
        "GET",
        r"/installation/repositories",
        {"repositories": [{"id": 5, "name": "api", "full_name": "acme/api", "default_branch": "main"}]},
    )
    from apps.repositories import services

    original = services.sync_github
    monkeypatch.setattr(
        "apps.repositories.views.services.sync_github", lambda c: original(c, router.client())
    )
    from apps.git_providers.github.client import clear_token_cache

    clear_token_cache()
    assert api.post("/api/v1/integrations/github/sync").json() == {"installations": 1, "repositories": 1}
    assert api.get("/api/v1/repositories").json()[0]["full_name"] == "acme/api"


# --- pull requests & reviews ---------------------------------------------------------------------


def test_pull_request_list_pagination_and_filters(api, fake_credential):
    repo_a = make_repository(credential=fake_credential, name="a")
    repo_b = make_repository(credential=fake_credential, name="b")
    for n in range(1, 121):
        make_pull_request(repo_a if n % 2 else repo_b, n)
    failed_pr = make_pull_request(repo_a, 500)
    ReviewRun.objects.create(pull_request=failed_pr, trigger="manual", head_sha="x", status="failed")

    first = api.get("/api/v1/pull-requests").json()
    assert len(first["results"]) == 50 and first["next"]
    ids = [r["id"] for r in first["results"]]
    url = first["next"]
    while url:
        page = api.get(url).json()
        ids += [r["id"] for r in page["results"]]
        url = page["next"]
    assert len(ids) == 121 == len(set(ids))

    filtered = api.get("/api/v1/pull-requests", {"repository": repo_a.pk, "review_status": "failed"}).json()
    assert [r["number"] for r in filtered["results"]] == [500]
    assert filtered["results"][0]["latest_review"]["status"] == "failed"
    none = api.get("/api/v1/pull-requests", {"repository": repo_b.pk, "review_status": "none"}).json()
    assert len(none["results"]) == 50


def test_latest_review_counts(api, fake_credential):
    pr = make_pull_request(make_repository(credential=fake_credential), 1)
    run = ReviewRun.objects.create(pull_request=pr, trigger="manual", head_sha="x", status="completed")
    for severity, status in [
        ("high", "posted"),
        ("high", "in_summary"),
        ("low", "below_threshold"),
        ("critical", "posted"),
    ]:
        Finding.objects.create(
            review_run=run,
            fingerprint="f",
            path="a",
            category="bug",
            severity=severity,
            title="t",
            body="b",
            post_status=status,
        )
    item = api.get("/api/v1/pull-requests").json()["results"][0]
    assert item["latest_review"]["counts"] == {"critical": 1, "high": 2, "medium": 0, "low": 0, "info": 0}
    assert item["latest_review"]["posted_count"] == 2


def test_manual_rerun_and_conflict(api, fake_credential, no_enqueue, django_capture_on_commit_callbacks):
    pr = make_pull_request(make_repository(credential=fake_credential), 1)
    with django_capture_on_commit_callbacks(execute=True):
        response = api.post(f"/api/v1/pull-requests/{pr.pk}/reviews")
    assert response.status_code == 201
    assert response.json()["status"] == "queued" and response.json()["trigger"] == "manual"
    assert no_enqueue == [response.json()["id"]]
    assert api.post(f"/api/v1/pull-requests/{pr.pk}/reviews").status_code == 409
    history = api.get(f"/api/v1/pull-requests/{pr.pk}/reviews").json()
    assert len(history) == 1


def test_manual_rerun_rejected_for_disabled_repo(api, fake_credential, no_enqueue):
    pr = make_pull_request(make_repository(credential=fake_credential, enabled=False), 1)
    assert api.post(f"/api/v1/pull-requests/{pr.pk}/reviews").status_code == 400


def test_manual_rerun_allowed_when_auto_review_off(api, fake_credential, no_enqueue):
    pr = make_pull_request(make_repository(credential=fake_credential, auto_review=False), 1)
    assert api.post(f"/api/v1/pull-requests/{pr.pk}/reviews").status_code == 201


def test_review_detail(api, fake_credential):
    pr = make_pull_request(make_repository(credential=fake_credential), 1)
    run = ReviewRun.objects.create(
        pull_request=pr,
        trigger="manual",
        head_sha="x",
        status="failed",
        stage="llm",
        error="All LLM requests failed.",
        files_ignored=[{"path": "yarn.lock", "reason": "ignore_pattern", "pattern": "yarn.lock"}],
    )
    Finding.objects.create(
        review_run=run,
        fingerprint="f",
        path="a.py",
        line_start=3,
        line_end=3,
        category="bug",
        severity="high",
        title="t",
        body="b",
        post_status="duplicate",
    )
    data = api.get(f"/api/v1/reviews/{run.pk}").json()
    assert data["stage"] == "llm" and data["stage_label"] == "Calling LLM"
    assert data["error"] == "All LLM requests failed."
    assert data["findings"][0]["post_status_label"] == "Already posted on this PR"
    assert data["files_ignored"][0]["pattern"] == "yarn.lock"
    assert data["pull_request"]["number"] == 1


def test_usage_endpoint(api, fake_credential):
    repo = make_repository(credential=fake_credential)
    for tokens in (100, 250):
        LLMUsage.objects.create(
            credential=fake_credential,
            repository=repo,
            provider="fake",
            model="demo",
            input_tokens=tokens,
            output_tokens=10,
            status="ok",
        )
    data = api.get("/api/v1/usage", {"group_by": "credential,model"}).json()
    assert data["totals"] == {"input_tokens": 350, "output_tokens": 20, "requests": 2}
    assert data["rows"][0]["credential__name"] == "Demo"
    assert api.get("/api/v1/usage", {"group_by": "bogus"}).status_code == 400


def test_openapi_schema_renders(api):
    response = api.get("/api/v1/schema/")
    assert response.status_code == 200
