"""Batch D: notification channels, events, delivery, digest, and SSRF protection."""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
from datetime import timedelta
from decimal import Decimal

import httpx2
import pytest
from django.core import mail
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.credentials import budgets
from apps.git_providers.fake import FakeGitProvider
from apps.notifications import senders, services
from apps.notifications.messages import Message, slack_escape, to_slack
from apps.notifications.models import Event, NotificationChannel, NotificationDelivery
from apps.reviews import pipeline
from apps.reviews.models import Finding, LLMUsage, ReviewRun
from apps.reviews.services import create_run
from tests.factories import ScriptedLLM, added_file, finding, make_pull_request, make_repository, pr_info
from tests.http import MockRouter

SLACK_URL = "https://hooks.slack.com/services/T000/B000/secretsecret"
HOOK_URL = "https://hooks.example.com/reviewbot"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Every host resolves to a public address unless a test says otherwise."""
    answers = {"default": "52.1.2.3"}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        address = answers.get(host, answers["default"])
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr("apps.notifications.urlguard.socket.getaddrinfo", fake_getaddrinfo)
    return answers


@pytest.fixture
def router(monkeypatch):
    r = MockRouter()
    r.add("POST", r"hooks\.slack\.com", "ok")
    r.add("POST", r"hooks\.example\.com", {"ok": True})
    monkeypatch.setattr(senders, "http_client", lambda: r.client())
    return r


def channel(kind="slack", events=(Event.HIGH_RISK,), **kwargs) -> NotificationChannel:
    url = kwargs.pop("url", SLACK_URL if kind == "slack" else HOOK_URL)
    secret = kwargs.pop("secret", "")
    ch = NotificationChannel(name=kwargs.pop("name", kind), kind=kind, events=list(events), **kwargs)
    if kind != "email":
        ch.set_url(url)
    ch.set_secret(secret)
    ch.save()
    return ch


# --- rendering ---------------------------------------------------------------------------------


def test_slack_escapes_mentions_and_links():
    assert slack_escape("<!channel> & <https://evil|x>") == "&lt;!channel&gt; &amp; &lt;https://evil|x&gt;"
    payload = to_slack(Message(event="x", title="Fix <!here>", url="https://rb.example/reviews/1"))
    blocks = payload["attachments"][0]["blocks"]
    assert blocks[0]["text"]["text"] == "*Fix &lt;!here&gt;*"
    assert "<https://rb.example/reviews/1|Open in Reviewbot>" in json.dumps(blocks)


def test_message_round_trip():
    message = Message(event="e", title="t", fields=[("a", "b")], data={"k": 1})
    assert Message.from_dict(json.loads(json.dumps(message.as_dict()))) == message


# --- events from reviews -------------------------------------------------------------------------


@pytest.fixture
def repo(fake_credential):
    return make_repository(credential=fake_credential, name="app")


def run_review(repo, findings, django_capture_on_commit_callbacks):
    git = FakeGitProvider()
    git.add_pull_request("acme/app", pr_info(1), [added_file("src/a.py", [f"x{i} = {i}" for i in range(5)])])
    pr = repo.pull_requests.filter(number=1).first() or make_pull_request(repo, 1)
    run = create_run(pr, trigger=ReviewRun.Trigger.WEBHOOK, head_sha="a" * 40)
    with django_capture_on_commit_callbacks(execute=True):
        pipeline.execute(run.pk, git=git, llm=ScriptedLLM([{"summary": "", "findings": findings}]))
    run.refresh_from_db()
    return run


@pytest.mark.django_db
def test_high_risk_review_notifies_slack(repo, router, django_capture_on_commit_callbacks):
    ch = channel(min_risk=101, min_severity="critical")
    run = run_review(
        repo,
        [finding("src/a.py", 2, severity="critical", title="SQL <!channel>")],
        django_capture_on_commit_callbacks,
    )
    delivery = NotificationDelivery.objects.get()
    assert (delivery.channel, delivery.event, delivery.status, delivery.dedup_key) == (
        ch,
        Event.HIGH_RISK,
        "sent",
        f"run:{run.pk}",
    )
    sent = json.loads(router.requests[0].content)
    assert "High-risk pull request: acme/app#1" in sent["text"]
    assert "1 critical" in json.dumps(sent)


@pytest.mark.django_db
def test_low_risk_review_does_not_notify(repo, router, django_capture_on_commit_callbacks):
    channel(min_risk=90, min_severity="critical")
    run_review(repo, [finding("src/a.py", 2, severity="low")], django_capture_on_commit_callbacks)
    assert NotificationDelivery.objects.count() == 0 and router.requests == []


@pytest.mark.django_db
def test_repository_filter_and_disabled_channels(
    repo, router, django_capture_on_commit_callbacks, fake_credential
):
    other = make_repository(credential=fake_credential, name="other")
    scoped = channel(min_risk=0)
    scoped.repositories.set([other])
    channel(min_risk=0, enabled=False, name="off")
    run_review(repo, [finding("src/a.py", 2)], django_capture_on_commit_callbacks)
    assert NotificationDelivery.objects.count() == 0


@pytest.mark.django_db
def test_failed_review_notifies_signed_webhook(
    repo, router, django_capture_on_commit_callbacks, fake_credential
):
    channel(kind="webhook", events=[Event.REVIEW_FAILED], secret="s3cret")
    fake_credential.status = "invalid"
    fake_credential.save()
    run = run_review(repo, [], django_capture_on_commit_callbacks)
    assert run.status == "failed"
    request = router.requests[0]
    body = request.content
    assert request.headers["X-Reviewbot-Event"] == "review.failed"
    expected = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert request.headers["X-Reviewbot-Signature-256"] == expected
    payload = json.loads(body)
    assert payload["event"] == "review.failed" and payload["data"]["reason"] == "no_credential"


@pytest.mark.django_db
def test_each_review_notifies_once(repo, router, django_capture_on_commit_callbacks):
    channel(min_risk=0)
    run = run_review(repo, [finding("src/a.py", 2)], django_capture_on_commit_callbacks)
    with django_capture_on_commit_callbacks(execute=True):
        assert services.on_review_finished(run) == 0
    assert NotificationDelivery.objects.count() == 1


@pytest.mark.django_db
def test_budget_threshold_notifies(fake_credential, router, django_capture_on_commit_callbacks):
    channel(events=[Event.BUDGET])
    fake_credential.monthly_budget_usd = Decimal("1")
    fake_credential.save()
    LLMUsage.objects.create(
        credential=fake_credential, provider="fake", model="m", input_tokens=1, output_tokens=1,
        cost_usd=Decimal("0.9"), status="ok",
    )  # fmt: skip
    with django_capture_on_commit_callbacks(execute=True):
        budgets.check_thresholds(fake_credential)
    delivery = NotificationDelivery.objects.get()
    assert delivery.event == Event.BUDGET and "80%" in delivery.title and delivery.status == "sent"


# --- delivery failures ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_permanent_failure_is_recorded(monkeypatch, django_capture_on_commit_callbacks):
    r = MockRouter()
    r.add("POST", r"hooks\.slack\.com", "invalid_token", status=403)
    monkeypatch.setattr(senders, "http_client", lambda: r.client())
    channel(events=[Event.DIGEST])
    with django_capture_on_commit_callbacks(execute=True):
        services.send_weekly_digest()
    delivery = NotificationDelivery.objects.get()
    assert delivery.status == "failed" and "HTTP 403" in delivery.error and delivery.attempts == 1


@pytest.mark.django_db
def test_transient_failures_are_retried(monkeypatch, django_capture_on_commit_callbacks):
    statuses = iter([503, 200])
    r = MockRouter()
    r.add("POST", r"hooks\.slack\.com", handler=lambda _req: httpx2.Response(next(statuses), text="x"))
    monkeypatch.setattr(senders, "http_client", lambda: r.client())
    channel(events=[Event.DIGEST])
    with django_capture_on_commit_callbacks(execute=True):
        services.send_weekly_digest()
    delivery = NotificationDelivery.objects.get()
    assert delivery.status == "sent" and delivery.attempts == 2


@pytest.mark.django_db
def test_private_addresses_are_refused_at_send_time(router, public_dns, django_capture_on_commit_callbacks):
    channel(kind="webhook", events=[Event.DIGEST])
    public_dns["hooks.example.com"] = "10.0.0.7"  # DNS changed after the channel was saved
    with django_capture_on_commit_callbacks(execute=True):
        services.send_weekly_digest()
    delivery = NotificationDelivery.objects.get()
    assert delivery.status == "failed" and "private" in delivery.error
    assert router.requests == []


@pytest.mark.django_db
def test_email_channel(django_capture_on_commit_callbacks):
    channel(kind="email", events=[Event.DIGEST], recipients=["lead@example.com"])
    with django_capture_on_commit_callbacks(execute=True):
        services.send_weekly_digest()
    assert mail.outbox[0].to == ["lead@example.com"]
    assert mail.outbox[0].subject.startswith("[Reviewbot] Reviewbot weekly digest")


# --- digest --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_digest_content(repo):
    pr = make_pull_request(repo, 3, title="Risky change")
    run = ReviewRun.objects.create(
        pull_request=pr, trigger="webhook", head_sha="x", status="completed", risk_score=77
    )
    ReviewRun.objects.create(pull_request=pr, trigger="push", head_sha="y", status="failed")
    Finding.objects.create(
        review_run=run, fingerprint="f", path="a.py", category="bug", severity="high", title="t", body="b",
        post_status="posted", state="accepted", state_changed_at=timezone.now(),
    )  # fmt: skip
    LLMUsage.objects.create(
        provider="fake", model="m", input_tokens=1, output_tokens=1, cost_usd=Decimal("1.234"), status="ok"
    )
    message = services.build_digest()
    fields = dict(message.fields)
    assert fields["Reviews"] == "1 on 1 pull requests (1 failed)"
    assert fields["Findings"] == "1 high"
    assert fields["Triage"] == "1 accepted, 0 dismissed (acceptance 100%)"
    assert fields["LLM cost"] == "$1.23"
    assert message.lines == ["77/100 acme/app#3 Risky change"]
    old = services.build_digest(timezone.now() + timedelta(days=30))
    assert old.lines == [] and dict(old.fields)["Reviews"].startswith("0 on 0")


@pytest.mark.django_db
def test_digest_is_sent_once_per_week(router, django_capture_on_commit_callbacks):
    channel(events=[Event.DIGEST])
    with django_capture_on_commit_callbacks(execute=True):
        assert services.send_weekly_digest() == 1
        assert services.send_weekly_digest() == 0


# --- API -----------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_channel_api(api, router, public_dns):
    response = api.post(
        "/api/v1/notification-channels",
        {"name": "Eng", "kind": "slack", "url": SLACK_URL, "events": ["review.high_risk", "digest.weekly"]},
        format="json",
    )
    assert response.status_code == 201, response.content
    data = response.json()
    assert "url" not in data and data["url_hint"] == "hooks.slack.com/…cret"
    assert NotificationChannel.objects.get().url == SLACK_URL  # stored encrypted
    assert "secretsecret" not in NotificationChannel.objects.get().encrypted_url

    assert api.post(f"/api/v1/notification-channels/{data['id']}/test").json() == {"ok": True, "error": ""}
    patched = api.patch(f"/api/v1/notification-channels/{data['id']}", {"enabled": False}, format="json")
    assert patched.json()["enabled"] is False
    assert {"notification.channel_created", "notification.channel_updated"} <= set(
        AuditEvent.objects.values_list("action", flat=True)
    )
    assert "secretsecret" not in json.dumps(list(AuditEvent.objects.values_list("metadata", flat=True)))
    assert api.get(f"/api/v1/notification-channels/{data['id']}/deliveries").json() == []
    assert len(api.get("/api/v1/notification-channels/events").json()) == 4
    assert api.get("/api/v1/notification-channels/digest-preview").json()["event"] == "digest.weekly"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"kind": "slack", "url": "https://example.com/hook"}, "url"),
        ({"kind": "slack", "url": "http://hooks.slack.com/services/x"}, "url"),
        ({"kind": "webhook", "url": "https://internal.example.com/x"}, "url"),
        ({"kind": "webhook"}, "url"),
        ({"kind": "email", "recipients": []}, "recipients"),
        ({"kind": "email", "recipients": ["a@example.com"], "events": []}, "events"),
    ],
)
def test_channel_validation(api, public_dns, body, field):
    public_dns["internal.example.com"] = "192.168.1.5"
    payload = {"name": "x", "events": ["review.failed"], **body}
    response = api.post("/api/v1/notification-channels", payload, format="json")
    assert response.status_code == 400
    assert field in response.json()["error"]["details"]


@pytest.mark.django_db
def test_private_webhooks_can_be_allowed(api, public_dns, settings):
    settings.ALLOW_PRIVATE_WEBHOOKS = True
    public_dns["chat.internal"] = "10.1.1.1"
    response = api.post(
        "/api/v1/notification-channels",
        {"name": "x", "kind": "webhook", "url": "http://chat.internal/hook", "events": ["review.failed"]},
        format="json",
    )
    assert response.status_code == 201


@pytest.mark.django_db
def test_channels_are_admin_only(viewer_client):
    assert viewer_client.get("/api/v1/notification-channels").status_code == 403


@pytest.fixture
def viewer_client(db):
    from rest_framework.test import APIClient

    from apps.accounts.models import User

    client = APIClient()
    client.force_login(User.objects.create_user("v@example.com", "correct-horse-battery", role="viewer"))
    return client
