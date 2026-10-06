from __future__ import annotations

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone

from apps.credentials import crypto


class Event(models.TextChoices):
    HIGH_RISK = "review.high_risk", "High-risk pull request"
    REVIEW_FAILED = "review.failed", "Review failed"
    BUDGET = "budget.threshold", "LLM budget threshold reached"
    DIGEST = "digest.weekly", "Weekly digest"


class NotificationChannel(models.Model):
    """Where notifications go: a Slack incoming webhook, email recipients, or a signed JSON webhook."""

    class Kind(models.TextChoices):
        SLACK = "slack", "Slack"
        EMAIL = "email", "Email"
        WEBHOOK = "webhook", "Webhook"

    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    enabled = models.BooleanField(default=True)
    encrypted_url = models.TextField(blank=True)
    encrypted_secret = models.TextField(blank=True, help_text="HMAC signing secret (webhook channels).")
    url_hint = models.CharField(max_length=120, blank=True)
    recipients = ArrayField(models.EmailField(), default=list, blank=True)
    events = ArrayField(models.CharField(max_length=32, choices=Event.choices), default=list)
    # A review is "high risk" when its risk score or one of its reported findings reaches these thresholds.
    min_risk = models.PositiveSmallIntegerField(default=60)
    min_severity = models.CharField(max_length=16, blank=True, default="critical")
    repositories = models.ManyToManyField(
        "repositories.Repository", blank=True, related_name="+", help_text="Empty means every repository."
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return f"{self.kind}:{self.name}"

    @property
    def audit_label(self) -> str:
        return f"{self.name} ({self.get_kind_display()})"

    @property
    def url(self) -> str:
        return crypto.decrypt(self.encrypted_url) if self.encrypted_url else ""

    @property
    def secret(self) -> str:
        return crypto.decrypt(self.encrypted_secret) if self.encrypted_secret else ""

    def set_url(self, url: str) -> None:
        self.encrypted_url = crypto.encrypt(url) if url else ""
        self.url_hint = _hint(url)

    def set_secret(self, secret: str) -> None:
        self.encrypted_secret = crypto.encrypt(secret) if secret else ""


def _hint(url: str) -> str:
    """Host plus the last 4 characters, enough to recognise a URL without revealing its token."""
    if not url:
        return ""
    host = url.split("://", 1)[-1].split("/", 1)[0]
    return f"{host}/…{url[-4:]}"


class NotificationDelivery(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    channel = models.ForeignKey(NotificationChannel, on_delete=models.CASCADE, related_name="deliveries")
    event = models.CharField(max_length=32)
    # Prevents duplicates, e.g. "run:42" for a review or "digest:2026-W40".
    dedup_key = models.CharField(max_length=100)
    title = models.CharField(max_length=300)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    error = models.CharField(max_length=500, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "event", "dedup_key"], name="uniq_notification_delivery"
            )
        ]
        indexes = [models.Index(fields=["created_at"])]

    def __str__(self) -> str:
        return f"{self.event}:{self.dedup_key}"
