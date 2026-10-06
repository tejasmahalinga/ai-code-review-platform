from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.credentials import crypto


class LLMCredential(models.Model):
    class Provider(models.TextChoices):
        OPENAI = "openai", "OpenAI"
        ANTHROPIC = "anthropic", "Anthropic"
        OPENAI_COMPATIBLE = "openai_compatible", "OpenAI-compatible"
        FAKE = "fake", "Demo"

    class Status(models.TextChoices):
        VALID = "valid", "Valid"
        INVALID = "invalid", "Invalid"
        REVOKED = "revoked", "Revoked"

    name = models.CharField(max_length=100)
    provider = models.CharField(max_length=32, choices=Provider.choices)
    base_url = models.URLField(max_length=500, blank=True)
    encrypted_secret = models.TextField(blank=True)
    last4 = models.CharField(max_length=4, blank=True)
    default_model = models.CharField(max_length=200)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.VALID)
    status_message = models.CharField(max_length=500, blank=True)
    last_validated_at = models.DateTimeField(null=True, blank=True)
    monthly_budget_usd = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.provider})"

    def set_secret(self, plaintext: str) -> None:
        self.encrypted_secret = crypto.encrypt(plaintext) if plaintext else ""
        self.last4 = plaintext[-4:] if len(plaintext) >= 8 else ""

    def get_secret(self) -> str:
        return crypto.decrypt(self.encrypted_secret) if self.encrypted_secret else ""

    @property
    def is_usable(self) -> bool:
        return self.status == self.Status.VALID

    def revoke(self) -> None:
        self.status = self.Status.REVOKED
        self.revoked_at = timezone.now()
        self.encrypted_secret = ""
        self.save(update_fields=["status", "revoked_at", "encrypted_secret"])


class ModelPrice(models.Model):
    """USD price per million tokens for models whose name starts with ``model_prefix`` (KEY-05).

    An empty ``provider`` applies to every provider; an empty prefix to every model of the provider. The most
    specific row wins: provider-specific before generic, then the longest prefix.
    """

    provider = models.CharField(max_length=32, blank=True)
    model_prefix = models.CharField(max_length=200, blank=True)
    input_usd_per_mtok = models.DecimalField(max_digits=10, decimal_places=4)
    output_usd_per_mtok = models.DecimalField(max_digits=10, decimal_places=4)
    is_default = models.BooleanField(
        default=False, help_text="Shipped with Reviewbot rather than added by an admin."
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider", "model_prefix"]
        constraints = [
            models.UniqueConstraint(fields=["provider", "model_prefix"], name="uniq_model_price"),
        ]

    def __str__(self) -> str:
        return f"{self.provider or '*'}:{self.model_prefix or '*'}"

    @property
    def audit_label(self) -> str:
        return f"{self.provider or 'any provider'} / {self.model_prefix or 'any model'}"


class BudgetAlert(models.Model):
    """Records that a monthly budget threshold was reached, so each alert fires once per month."""

    credential = models.ForeignKey(LLMCredential, on_delete=models.CASCADE, related_name="budget_alerts")
    month = models.DateField(help_text="First day of the month (UTC).")
    threshold = models.PositiveSmallIntegerField()
    spent_usd = models.DecimalField(max_digits=12, decimal_places=6)
    budget_usd = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(fields=["credential", "month", "threshold"], name="uniq_budget_alert"),
        ]

    def __str__(self) -> str:
        return f"budget:{self.credential_id}:{self.month}:{self.threshold}"
