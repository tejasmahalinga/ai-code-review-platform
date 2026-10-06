"""Batch C: pricing (KEY-05), budgets, usage analytics (ADM-05), personal API tokens (ADM-06)."""

from __future__ import annotations

import csv
import io
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import ApiToken, User
from apps.audit.models import AuditEvent
from apps.credentials import budgets, pricing
from apps.credentials.models import BudgetAlert, ModelPrice
from apps.git_providers.fake import FakeGitProvider
from apps.reviews import pipeline
from apps.reviews.models import LLMUsage, ReviewRun
from apps.reviews.services import create_run
from tests.factories import ScriptedLLM, added_file, finding, make_pull_request, make_repository, pr_info

REPO = "acme/app"
SOURCE = [added_file("src/foo.py", [f"x{i} = {i}" for i in range(10)])]


def price(provider="fake", prefix="test-model", inp="1000", out="2000") -> ModelPrice:
    return ModelPrice.objects.create(
        provider=provider,
        model_prefix=prefix,
        input_usd_per_mtok=Decimal(inp),
        output_usd_per_mtok=Decimal(out),
    )


def usage(credential, cost, *, when=None, model="test-model", repository=None) -> LLMUsage:
    return LLMUsage.objects.create(
        credential=credential,
        repository=repository,
        provider=credential.provider,
        model=model,
        input_tokens=1000,
        output_tokens=100,
        cost_usd=cost,
        status="ok",
        created_at=when or timezone.now(),
    )


# --- pricing -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_defaults_are_installed_and_longest_prefix_wins():
    assert ModelPrice.objects.filter(is_default=True).count() == len(pricing.DEFAULT_PRICES)
    assert pricing.price_for("openai", "gpt-4o-mini-2024-07-18").model_prefix == "gpt-4o-mini"
    assert pricing.price_for("openai", "gpt-4o-2024-08-06").model_prefix == "gpt-4o"
    assert pricing.price_for("anthropic", "claude-sonnet-4-5-20250929").model_prefix == "claude-sonnet-4"
    assert pricing.price_for("openai_compatible", "llama3.1:70b") is None
    generic = price(provider="", prefix="llama", inp="0", out="0")
    assert pricing.price_for("openai_compatible", "llama3.1:70b") == generic
    specific = price(provider="openai_compatible", prefix="llama", inp="1", out="1")
    assert (
        pricing.price_for("openai_compatible", "llama3.1:70b") == specific
    )  # provider-specific beats generic


def test_cost_arithmetic():
    p = ModelPrice(input_usd_per_mtok=Decimal("2.50"), output_usd_per_mtok=Decimal("10"))
    assert pricing.cost(p, 1_000_000, 100_000) == Decimal("3.500000")
    assert pricing.cost(p, 1234, 56) == Decimal("0.003645")
    assert pricing.cost(None, 1, 1) is None


@pytest.mark.django_db
def test_install_defaults_never_overwrites_edits():
    row = ModelPrice.objects.get(provider="openai", model_prefix="gpt-4o")
    row.input_usd_per_mtok = Decimal("9")
    row.is_default = False
    row.save()
    ModelPrice.objects.filter(provider="openai", model_prefix="gpt-4o-mini").delete()
    assert pricing.install_defaults() == 1
    assert ModelPrice.objects.get(provider="openai", model_prefix="gpt-4o").input_usd_per_mtok == Decimal("9")


# --- pipeline: cost and budgets ------------------------------------------------------------------


@pytest.fixture
def repo(fake_credential):
    return make_repository(credential=fake_credential, name="app")


def review(repo, *, trigger=ReviewRun.Trigger.WEBHOOK, user=None, script=None):
    git = FakeGitProvider()
    git.add_pull_request(REPO, pr_info(1), SOURCE)
    pr = repo.pull_requests.filter(number=1).first() or make_pull_request(repo, 1)
    run = create_run(pr, trigger=trigger, head_sha="a" * 40, created_by=user)
    llm = ScriptedLLM(script or [{"summary": "", "findings": [finding("src/foo.py", 2)]}])
    pipeline.execute(run.pk, git=git, llm=llm)
    run.refresh_from_db()
    return run, git, llm


@pytest.mark.django_db
def test_review_cost_is_recorded(repo):
    price()
    run, _, _ = review(repo)
    row = LLMUsage.objects.get(review_run=run)
    # 100 input and 20 output tokens at $1000 / $2000 per million.
    assert row.cost_usd == Decimal("0.140000")
    assert run.cost_usd == Decimal("0.140000")


@pytest.mark.django_db
def test_unpriced_model_has_unknown_cost(repo):
    ModelPrice.objects.filter(provider="fake").delete()
    run, _, _ = review(repo)
    assert LLMUsage.objects.get(review_run=run).cost_usd is None
    assert run.cost_usd is None


@pytest.mark.django_db
def test_budget_alerts_fire_once_per_threshold(repo, fake_credential, admin_user, settings):
    fake_credential.monthly_budget_usd = Decimal("1.00")
    fake_credential.save()
    usage(fake_credential, Decimal("0.85"))
    assert budgets.check_thresholds(fake_credential) == [80]
    assert budgets.check_thresholds(fake_credential) == []
    usage(fake_credential, Decimal("0.20"))
    assert budgets.check_thresholds(fake_credential) == [100]
    assert BudgetAlert.objects.count() == 2
    assert [
        e.metadata["threshold"] for e in AuditEvent.objects.filter(action="budget.threshold_reached")
    ] == [100, 80]
    assert len(mail.outbox) == 2 and "100%" in mail.outbox[1].subject
    # Last month's spending does not count.
    LLMUsage.objects.update(created_at=timezone.now() - timedelta(days=40))
    assert budgets.status(fake_credential).state == "ok"


@pytest.mark.django_db
def test_exceeded_budget_pauses_automatic_reviews(repo, fake_credential, admin_user):
    fake_credential.monthly_budget_usd = Decimal("1.00")
    fake_credential.save()
    usage(fake_credential, Decimal("1.50"))

    run, git, llm = review(repo)
    assert (run.status, run.status_reason) == ("skipped", "budget_exceeded")
    assert llm.calls == []
    assert "monthly budget" in git.posted[0].body
    assert git.check_runs[0].conclusion == "skipped"

    push_run, push_git, _ = review(repo, trigger=ReviewRun.Trigger.PUSH)
    assert push_run.status_reason == "budget_exceeded" and push_git.posted == []  # no comment per push

    reviewer = User.objects.create_user("rev@example.com", "correct-horse-battery", role="reviewer")
    blocked, _, _ = review(repo, trigger=ReviewRun.Trigger.MANUAL, user=reviewer)
    assert blocked.status_reason == "budget_exceeded"

    allowed, _, llm = review(repo, trigger=ReviewRun.Trigger.MANUAL, user=admin_user)
    assert allowed.status == "completed" and len(llm.calls) == 1


@pytest.mark.django_db
def test_manual_review_api_refuses_reviewers_over_budget(repo, fake_credential, monkeypatch):
    monkeypatch.setattr("apps.reviews.services.enqueue", lambda *a, **k: None)
    fake_credential.monthly_budget_usd = Decimal("1.00")
    fake_credential.save()
    usage(fake_credential, Decimal("2"))
    pr = make_pull_request(repo, 7)
    reviewer = User.objects.create_user("rev@example.com", "correct-horse-battery", role="reviewer")
    client = APIClient()
    client.force_login(reviewer)
    response = client.post(f"/api/v1/pull-requests/{pr.pk}/reviews")
    assert response.status_code == 400 and "monthly budget" in response.json()["error"]["message"]


# --- API: budgets, prices, usage ----------------------------------------------------------------


@pytest.mark.django_db
def test_credential_budget_fields(api, fake_credential):
    usage(fake_credential, Decimal("3"))
    usage(fake_credential, None)
    response = api.patch(
        f"/api/v1/llm-credentials/{fake_credential.pk}", {"monthly_budget_usd": "10"}, format="json"
    )
    assert response.status_code == 200
    assert response.json()["budget"] == {
        "budget_usd": "10.00",
        "spent_usd": "3.00",
        "percent": 30.0,
        "state": "ok",
        "unpriced_requests": 1,
    }
    assert (
        api.patch(
            f"/api/v1/llm-credentials/{fake_credential.pk}", {"monthly_budget_usd": "0"}, format="json"
        ).status_code
        == 400
    )
    cleared = api.patch(
        f"/api/v1/llm-credentials/{fake_credential.pk}", {"monthly_budget_usd": None}, format="json"
    )
    assert cleared.json()["budget"]["state"] == "none"
    event = AuditEvent.objects.filter(action="credential.updated").first()
    assert event is not None and "monthly_budget_usd" in event.metadata["changes"]


@pytest.mark.django_db
def test_model_price_api_and_recalculate(api, fake_credential):
    usage(fake_credential, None, model="custom-7b")
    usage(fake_credential, None, model="other")
    created = api.post(
        "/api/v1/model-prices",
        {"provider": "fake", "model_prefix": "custom", "input_usd_per_mtok": "2", "output_usd_per_mtok": "4"},
        format="json",
    )
    assert created.status_code == 201 and created.json()["is_default"] is False
    assert (
        api.post(
            "/api/v1/model-prices",
            {"provider": "nope", "model_prefix": "x", "input_usd_per_mtok": "1", "output_usd_per_mtok": "1"},
            format="json",
        ).status_code
        == 400
    )
    # The shipped catch-all price for the demo provider would match "other"; remove it to leave one unpriced.
    ModelPrice.objects.filter(provider="fake", model_prefix="").delete()
    result = api.post("/api/v1/model-prices/recalculate").json()
    assert result == {"updated": 1, "unpriced_remaining": 1}
    assert LLMUsage.objects.get(model="custom-7b").cost_usd == Decimal("0.002400")
    assert {"pricing.created", "pricing.recalculated"} <= set(
        AuditEvent.objects.values_list("action", flat=True)
    )


@pytest.mark.django_db
def test_usage_cost_totals_and_csv(api, fake_credential, repo):
    usage(fake_credential, Decimal("0.5"), repository=repo)
    usage(fake_credential, Decimal("0.25"), repository=repo, model="other")
    usage(fake_credential, None, repository=repo)
    data = api.get("/api/v1/usage", {"group_by": "repository,model"}).json()
    assert Decimal(str(data["totals"]["cost_usd"])) == Decimal("0.75")
    assert data["totals"]["unpriced"] == 1
    response = api.get("/api/v1/usage", {"group_by": "day,model", "export": "csv"})
    assert response["Content-Type"].startswith("text/csv")
    assert "attachment" in response["Content-Disposition"]
    rows = list(csv.reader(io.StringIO(response.content.decode())))
    assert rows[0] == [
        "day",
        "model",
        "reviews",
        "requests",
        "errors",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "unpriced",
    ]
    assert len(rows) == 3


def test_csv_injection_is_neutralized():
    from apps.reviews.views import _csv_safe

    assert _csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert _csv_safe("acme/app") == "acme/app"


# --- personal API tokens ------------------------------------------------------------------------


@pytest.mark.django_db
def test_api_token_lifecycle(api, admin_user):
    created = api.post("/api/v1/auth/tokens", {"name": "CI export", "expires_in_days": 30}, format="json")
    assert created.status_code == 201
    token = created.json()["token"]
    assert token.startswith("rbt_") and ApiToken.objects.get().token_hash != token
    assert "token" not in api.get("/api/v1/auth/tokens").json()[0]

    bearer = APIClient(HTTP_AUTHORIZATION=f"Bearer {token}")
    assert bearer.get("/api/v1/auth/me").json()["email"] == admin_user.email
    assert bearer.get("/api/v1/usage").status_code == 200  # acts with the owner's role
    assert ApiToken.objects.get().last_used_at is not None
    # Unsafe methods work without CSRF, but a token cannot mint tokens or change the password.
    assert bearer.post("/api/v1/auth/tokens", {"name": "x"}, format="json").status_code == 403
    assert bearer.post("/api/v1/auth/password", {"new_password": "x" * 12}, format="json").status_code == 403

    api.delete(f"/api/v1/auth/tokens/{created.json()['id']}")
    assert bearer.get("/api/v1/auth/me").status_code == 401
    assert {"token.created", "token.revoked"} <= set(AuditEvent.objects.values_list("action", flat=True))


@pytest.mark.django_db
def test_api_token_rejections(admin_user, viewer_token):
    assert APIClient(HTTP_AUTHORIZATION="Bearer rbt_nope").get("/api/v1/auth/me").status_code == 401
    token, user = viewer_token
    client = APIClient(HTTP_AUTHORIZATION=f"Bearer {token}")
    assert client.get("/api/v1/pull-requests").status_code == 200
    assert client.get("/api/v1/usage").status_code == 403  # viewer role applies
    User.objects.filter(pk=user.pk).update(is_active=False)
    assert client.get("/api/v1/pull-requests").status_code == 401
    expired, plaintext = ApiToken.issue(user=admin_user, name="old", expires_in_days=1)
    ApiToken.objects.filter(pk=expired.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert APIClient(HTTP_AUTHORIZATION=f"Bearer {plaintext}").get("/api/v1/auth/me").status_code == 401


@pytest.fixture
def viewer_token(db):
    user = User.objects.create_user("view@example.com", "correct-horse-battery", role="viewer")
    _, token = ApiToken.issue(user=user, name="read", expires_in_days=None)
    return token, user


def test_tokens_are_redacted_from_logs():
    from apps.core.logging import redact_text

    assert "rbt_" not in redact_text("auth with rbt_abcdefghijklmnopqrstuvwxyz0123")
