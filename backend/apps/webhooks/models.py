from __future__ import annotations

from django.db import models
from django.utils import timezone


class WebhookDelivery(models.Model):
    """Every inbound webhook we accepted (signature-verified), for idempotency and debugging."""

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        PROCESSED = "processed", "Processed"
        IGNORED = "ignored", "Ignored"
        FAILED = "failed", "Failed"

    provider = models.CharField(max_length=16)
    delivery_id = models.CharField(max_length=100)
    event = models.CharField(max_length=64)
    action = models.CharField(max_length=64, blank=True)
    repository = models.ForeignKey(
        "repositories.Repository", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    payload = models.JSONField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RECEIVED)
    reason = models.CharField(max_length=300, blank=True)
    review_run = models.ForeignKey(
        "reviews.ReviewRun", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    received_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(fields=["provider", "delivery_id"], name="uniq_webhook_delivery"),
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.event}.{self.action}:{self.delivery_id}"
