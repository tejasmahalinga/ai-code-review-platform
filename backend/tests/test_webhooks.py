from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest

from apps.repositories.models import Installation, Repository
from apps.reviews.models import PullRequest, ReviewRun
from apps.webhooks.models import WebhookDelivery
from tests.factories import WEBHOOK_SECRET, make_connection, make_installation, make_repository

pytestmark = pytest.mark.django_db


@pytest.fixture
def enqueued(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr("apps.reviews.services.enqueue", calls.append)
    return calls


@pytest.fixture
def connection():
    return make_connection()


def post(client, event: str, payload: dict, *, secret: str = WEBHOOK_SECRET, delivery: str | None = None):
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/github",
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=signature,
        HTTP_X_GITHUB_EVENT=event,
        HTTP_X_GITHUB_DELIVERY=delivery or str(uuid.uuid4()),
    )


def pr_payload(
    repo: Repository, action: str = "opened", *, draft: bool = False, sha: str = "a" * 40, number: int = 7
):
    return {
        "action": action,
        "installation": {"id": repo.installation.external_id},
        "repository": {"id": repo.external_id, "full_name": repo.full_name},
        "pull_request": {
            "number": number,
            "title": "Add search",
            "user": {"login": "octocat"},
            "state": "open",
            "draft": draft,
            "html_url": f"https://github.com/{repo.full_name}/pull/{number}",
            "base": {"ref": "main", "sha": "b" * 40},
            "head": {"ref": "feature", "sha": sha},
        },
    }


def test_invalid_signature_is_rejected_and_not_stored(anon, connection, enqueued):
    repo = make_repository()
    response = post(anon, "pull_request", pr_payload(repo), secret="wrong")
    assert response.status_code == 401
    assert WebhookDelivery.objects.count() == 0
    assert enqueued == []


def test_missing_signature_is_rejected(anon, connection):
    response = anon.post(
        "/webhooks/github",
        data=b"{}",
        content_type="application/json",
        HTTP_X_GITHUB_EVENT="ping",
        HTTP_X_GITHUB_DELIVERY="1",
    )
    assert response.status_code == 401


def test_webhook_404_when_github_not_configured(anon):
    assert anon.post("/webhooks/github", data=b"{}", content_type="application/json").status_code == 404


def test_pull_request_opened_queues_review(
    anon, connection, fake_credential, enqueued, django_capture_on_commit_callbacks
):
    repo = make_repository(credential=fake_credential)
    with django_capture_on_commit_callbacks(execute=True):
        response = post(anon, "pull_request", pr_payload(repo), delivery="d-1")
    assert response.status_code == 202
    run = ReviewRun.objects.get()
    assert run.status == "queued" and run.trigger == "webhook"
    assert run.settings_snapshot["model"] == "demo"
    assert enqueued == [run.pk]
    delivery = WebhookDelivery.objects.get()
    assert (delivery.status, delivery.reason, delivery.review_run_id) == (
        "processed",
        "review_queued",
        run.pk,
    )
    pr = PullRequest.objects.get()
    assert (pr.number, pr.title, pr.author_login) == (7, "Add search", "octocat")


def test_redelivery_does_not_create_second_run(anon, connection, fake_credential, enqueued):
    repo = make_repository(credential=fake_credential)
    post(anon, "pull_request", pr_payload(repo), delivery="same")
    response = post(anon, "pull_request", pr_payload(repo), delivery="same")
    assert response.json()["status"] == "duplicate"
    # Same commit delivered under a different id (e.g. reopened twice) is also idempotent.
    post(anon, "pull_request", pr_payload(repo, "reopened"), delivery="other")
    assert ReviewRun.objects.count() == 1
    assert WebhookDelivery.objects.get(delivery_id="other").reason == "already_reviewed_commit"


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ({"enabled": False}, "repository_disabled"),
        ({"auto_review": False}, "auto_review_disabled"),
        ({"draft": True}, "draft_pull_request"),
    ],
)
def test_ignored_events(anon, connection, fake_credential, enqueued, setup, reason):
    repo = make_repository(
        credential=fake_credential,
        enabled=setup.get("enabled", True),
        auto_review=setup.get("auto_review", True),
    )
    post(anon, "pull_request", pr_payload(repo, draft=setup.get("draft", False)))
    delivery = WebhookDelivery.objects.get()
    assert (delivery.status, delivery.reason) == ("ignored", reason)
    assert ReviewRun.objects.count() == 0


def test_drafts_reviewed_when_enabled(anon, connection, fake_credential, enqueued):
    repo = make_repository(credential=fake_credential, review_drafts=True)
    post(anon, "pull_request", pr_payload(repo, draft=True))
    assert ReviewRun.objects.count() == 1


def test_synchronize_updates_head_without_review(anon, connection, fake_credential, enqueued):
    repo = make_repository(credential=fake_credential)
    post(anon, "pull_request", pr_payload(repo))
    post(anon, "pull_request", pr_payload(repo, "synchronize", sha="c" * 40))
    assert PullRequest.objects.get().head_sha == "c" * 40
    assert ReviewRun.objects.count() == 1


def test_closed_event_records_merge(anon, connection, fake_credential, enqueued):
    repo = make_repository(credential=fake_credential)
    payload = pr_payload(repo, "closed")
    payload["pull_request"].update(state="closed", merged=True)
    post(anon, "pull_request", payload)
    assert PullRequest.objects.get().state == "merged"


def test_unknown_repository_ignored(anon, connection):
    payload = {"action": "opened", "installation": {"id": 1}, "repository": {"id": 999}, "pull_request": {}}
    post(anon, "pull_request", payload)
    assert WebhookDelivery.objects.get().reason == "unknown_repository"


def test_installation_created_syncs_repositories(anon, connection):
    payload = {
        "action": "created",
        "installation": {"id": 555, "account": {"login": "acme", "type": "Organization"}},
        "repositories": [
            {"id": 1, "name": "api", "full_name": "acme/api", "private": True},
            {"id": 2, "name": "web", "full_name": "acme/web", "private": False},
        ],
    }
    assert post(anon, "installation", payload).status_code == 202
    installation = Installation.objects.get(external_id=555)
    assert sorted(installation.repositories.values_list("full_name", flat=True)) == ["acme/api", "acme/web"]
    assert not Repository.objects.filter(enabled=True).exists()  # disabled until an admin opts in


def test_installation_repositories_added_and_removed(anon, connection):
    installation = make_installation(connection, external_id=555)
    repo = make_repository(installation=installation)
    payload = {
        "action": "added",
        "installation": {"id": 555, "account": {"login": "acme"}},
        "repositories_added": [{"id": 42, "name": "new", "full_name": "acme/new"}],
        "repositories_removed": [{"id": repo.external_id}],
    }
    post(anon, "installation_repositories", payload)
    repo.refresh_from_db()
    assert (repo.status, repo.enabled) == ("removed", False)
    assert Repository.objects.filter(full_name="acme/new", status="active").exists()


def test_installation_deleted_marks_repos_removed(anon, connection):
    installation = make_installation(connection, external_id=555)
    repo = make_repository(installation=installation)
    post(
        anon, "installation", {"action": "deleted", "installation": {"id": 555, "account": {"login": "acme"}}}
    )
    repo.refresh_from_db()
    installation.refresh_from_db()
    assert repo.status == "removed" and installation.removed_at is not None


def test_handler_failure_is_recorded_and_retriable(anon, connection, fake_credential, monkeypatch, enqueued):
    repo = make_repository(credential=fake_credential)

    def boom(*args, **kwargs):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr("apps.webhooks.github_handlers.handle", boom)
    response = post(anon, "pull_request", pr_payload(repo), delivery="retry-me")
    assert response.status_code == 500
    assert WebhookDelivery.objects.get().status == "failed"
    monkeypatch.undo()
    monkeypatch.setattr("apps.reviews.services.enqueue", enqueued.append)
    response = post(anon, "pull_request", pr_payload(repo), delivery="retry-me")
    assert response.status_code == 202
    assert ReviewRun.objects.count() == 1


def test_ping(anon, connection):
    assert post(anon, "ping", {"zen": "Keep it logically awesome."}).json()["status"] == "processed"


def test_delivery_log_endpoint(api, connection):
    from tests.factories import make_repository as _mk

    _mk()
    post(api, "ping", {"zen": "x"})
    data = api.get("/api/v1/webhook-deliveries").json()
    assert data["results"][0]["event"] == "ping"
