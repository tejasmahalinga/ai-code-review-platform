from __future__ import annotations

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone

from apps.credentials import crypto
from apps.reviews.engine.profiles import DEFAULT_PROFILE, PROFILE_CHOICES


class GitProviderConnection(models.Model):
    """Credentials of the Git provider integration (for GitHub: the GitHub App)."""

    class Provider(models.TextChoices):
        GITHUB = "github", "GitHub"
        GITLAB = "gitlab", "GitLab"
        BITBUCKET = "bitbucket", "Bitbucket"

    provider = models.CharField(max_length=16, choices=Provider.choices, unique=True)
    web_url = models.URLField(max_length=500)
    api_url = models.URLField(max_length=500)
    app_id = models.CharField(max_length=64, blank=True)
    app_slug = models.CharField(max_length=200, blank=True)
    app_name = models.CharField(max_length=200, blank=True)
    app_html_url = models.URLField(max_length=500, blank=True)
    client_id = models.CharField(max_length=200, blank=True)
    encrypted_private_key = models.TextField(blank=True)
    encrypted_webhook_secret = models.TextField(blank=True)
    encrypted_client_secret = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"{self.provider}:{self.app_slug or self.app_id}"

    @staticmethod
    def _enc(value: str) -> str:
        return crypto.encrypt(value) if value else ""

    @staticmethod
    def _dec(value: str) -> str:
        return crypto.decrypt(value) if value else ""

    def set_secrets(
        self, *, private_key: str = "", webhook_secret: str = "", client_secret: str = ""
    ) -> None:
        self.encrypted_private_key = self._enc(private_key)
        self.encrypted_webhook_secret = self._enc(webhook_secret)
        self.encrypted_client_secret = self._enc(client_secret)

    @property
    def private_key(self) -> str:
        return self._dec(self.encrypted_private_key)

    @property
    def webhook_secret(self) -> str:
        return self._dec(self.encrypted_webhook_secret)

    @property
    def install_url(self) -> str:
        if self.provider == self.Provider.GITHUB and self.app_slug:
            return f"{self.web_url}/apps/{self.app_slug}/installations/new"
        return ""


class Installation(models.Model):
    connection = models.ForeignKey(
        GitProviderConnection, on_delete=models.CASCADE, related_name="installations"
    )
    external_id = models.BigIntegerField()
    account_login = models.CharField(max_length=200)
    account_type = models.CharField(max_length=32, blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "external_id"], name="uniq_installation_external_id"
            )
        ]

    def __str__(self) -> str:
        return self.account_login

    @property
    def is_active(self) -> bool:
        return self.removed_at is None and self.suspended_at is None


class Repository(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        REMOVED = "removed", "Removed"

    installation = models.ForeignKey(Installation, on_delete=models.CASCADE, related_name="repositories")
    external_id = models.BigIntegerField()
    full_name = models.CharField(max_length=300)
    default_branch = models.CharField(max_length=255, blank=True)
    private = models.BooleanField(default=True)
    html_url = models.URLField(max_length=500, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["full_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["installation", "external_id"], name="uniq_repository_external_id"
            )
        ]
        indexes = [models.Index(fields=["external_id"])]

    def __str__(self) -> str:
        return self.full_name

    @property
    def owner(self) -> str:
        return self.full_name.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.full_name.split("/", 1)[1]

    @property
    def is_reviewable(self) -> bool:
        return self.enabled and self.status == self.Status.ACTIVE and self.installation.is_active


class Severity(models.TextChoices):
    CRITICAL = "critical", "Critical"
    HIGH = "high", "High"
    MEDIUM = "medium", "Medium"
    LOW = "low", "Low"
    INFO = "info", "Info"


SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}


class RepositorySettings(models.Model):
    repository = models.OneToOneField(Repository, on_delete=models.CASCADE, related_name="settings")
    auto_review = models.BooleanField(default=True)
    review_drafts = models.BooleanField(default=False)
    credential = models.ForeignKey(
        "credentials.LLMCredential",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="repository_settings",
    )
    model = models.CharField(
        max_length=200, blank=True, help_text="Overrides the credential's default model."
    )
    ignore_patterns = ArrayField(models.CharField(max_length=300), default=list, blank=True)
    replace_default_ignores = models.BooleanField(default=False)
    custom_instructions = models.TextField(blank=True, max_length=4000)
    min_severity = models.CharField(max_length=16, choices=Severity.choices, default=Severity.LOW)
    min_confidence = models.FloatField(default=0.5)
    max_inline_comments = models.PositiveIntegerField(default=25)
    max_changed_lines = models.PositiveIntegerField(default=2000)
    max_files = models.PositiveIntegerField(default=100)
    max_input_tokens = models.PositiveIntegerField(default=150_000, help_text="Hard cap per review run.")
    chunk_tokens = models.PositiveIntegerField(
        default=12_000, help_text="Target diff tokens per LLM request."
    )
    post_when_no_findings = models.BooleanField(default=True)
    profile = models.CharField(max_length=16, choices=PROFILE_CHOICES, default=DEFAULT_PROFILE)
    review_on_push = models.BooleanField(default=True, help_text="Review new commits pushed to open PRs.")
    check_runs = models.BooleanField(default=True, help_text="Report a GitHub check run per review.")
    gate_severity = models.CharField(
        max_length=16,
        choices=Severity.choices,
        blank=True,
        default="",
        help_text="Fail the check run when a finding at or above this severity is reported (empty = never).",
    )
    base_branch_patterns = ArrayField(
        models.CharField(max_length=255),
        default=list,
        blank=True,
        help_text="Only auto-review PRs whose base branch matches one of these globs (empty = all).",
    )
    rules = models.JSONField(default=list, blank=True, help_text="Structured review rules (RE-14).")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"settings:{self.repository_id}"

    @property
    def effective_model(self) -> str:
        if self.model:
            return self.model
        return self.credential.default_model if self.credential else ""

    def snapshot(self) -> dict[str, object]:
        return {
            "auto_review": self.auto_review,
            "review_drafts": self.review_drafts,
            "credential_id": self.credential_id,
            "model": self.effective_model,
            "ignore_patterns": list(self.ignore_patterns),
            "replace_default_ignores": self.replace_default_ignores,
            "custom_instructions": self.custom_instructions,
            "min_severity": self.min_severity,
            "min_confidence": self.min_confidence,
            "max_inline_comments": self.max_inline_comments,
            "max_changed_lines": self.max_changed_lines,
            "max_files": self.max_files,
            "max_input_tokens": self.max_input_tokens,
            "chunk_tokens": self.chunk_tokens,
            "post_when_no_findings": self.post_when_no_findings,
            "profile": self.profile,
            "review_on_push": self.review_on_push,
            "check_runs": self.check_runs,
            "gate_severity": self.gate_severity,
            "base_branch_patterns": list(self.base_branch_patterns),
            "rules": list(self.rules),
        }
