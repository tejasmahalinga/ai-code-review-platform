from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.repositories.models import Severity


class PullRequest(models.Model):
    class State(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"
        MERGED = "merged", "Merged"

    repository = models.ForeignKey(
        "repositories.Repository", on_delete=models.CASCADE, related_name="pull_requests"
    )
    number = models.PositiveIntegerField()
    title = models.CharField(max_length=500)
    author_login = models.CharField(max_length=200, blank=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.OPEN)
    is_draft = models.BooleanField(default=False)
    base_ref = models.CharField(max_length=255, blank=True)
    head_ref = models.CharField(max_length=255, blank=True)
    head_sha = models.CharField(max_length=64)
    html_url = models.URLField(max_length=500, blank=True)
    last_reviewed_sha = models.CharField(max_length=64, blank=True)
    reviews_paused = models.BooleanField(default=False, help_text="Set by `/reviewbot ignore`.")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["repository", "number"], name="uniq_pull_request_number")
        ]

    def __str__(self) -> str:
        return f"{self.repository_id}#{self.number}"

    @property
    def audit_label(self) -> str:
        return f"{self.repository.full_name}#{self.number}"


class ReviewRun(models.Model):
    class Trigger(models.TextChoices):
        WEBHOOK = "webhook", "Webhook"
        MANUAL = "manual", "Manual"
        PUSH = "push", "New commits"
        COMMAND = "command", "PR comment command"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"
        CANCELLED = "cancelled", "Cancelled"

    class Stage(models.TextChoices):
        PENDING = "pending", "Pending"
        FETCH_DIFF = "fetch_diff", "Fetching diff"
        FILTER = "filter", "Filtering files"
        CHUNK = "chunk", "Chunking"
        LLM = "llm", "Calling LLM"
        AGGREGATE = "aggregate", "Aggregating findings"
        POST_COMMENTS = "post_comments", "Posting comments"
        DONE = "done", "Done"
        TIMEOUT = "timeout", "Timed out"

    TERMINAL = {Status.COMPLETED, Status.FAILED, Status.SKIPPED, Status.CANCELLED}
    ACTIVE = {Status.QUEUED, Status.RUNNING}

    pull_request = models.ForeignKey(PullRequest, on_delete=models.CASCADE, related_name="review_runs")
    trigger = models.CharField(max_length=16, choices=Trigger.choices)
    head_sha = models.CharField(max_length=64)
    base_sha = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True)
    status_reason = models.CharField(max_length=64, blank=True)
    stage = models.CharField(max_length=16, choices=Stage.choices, default=Stage.PENDING)
    error = models.TextField(blank=True)
    idempotency_key = models.CharField(max_length=200, null=True, blank=True, unique=True)
    credential = models.ForeignKey(
        "credentials.LLMCredential",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="review_runs",
    )
    model = models.CharField(max_length=200, blank=True)
    settings_snapshot = models.JSONField(default=dict)
    files_reviewed = models.JSONField(default=list)
    files_ignored = models.JSONField(default=list)
    chunk_count = models.PositiveIntegerField(default=0)
    chunks_failed = models.PositiveIntegerField(default=0)
    incremental = models.BooleanField(
        default=False, help_text="Only changes since compare_base_sha were reviewed."
    )
    compare_base_sha = models.CharField(max_length=64, blank=True)
    check_run_id = models.CharField(max_length=64, blank=True)
    summary = models.TextField(blank=True)
    config_source = models.CharField(max_length=32, blank=True, help_text="dashboard or .reviewbot.yml")
    config_error = models.CharField(max_length=500, blank=True)
    risk_score = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="0-100, deterministic (RE-12)."
    )
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    # Sum of the LLM calls' cost; NULL when any call used a model without a price.
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    provider_review_id = models.CharField(max_length=64, blank=True)
    provider_review_url = models.URLField(max_length=500, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"run:{self.pk} {self.status}"

    @property
    def duration_ms(self) -> int | None:
        if self.started_at and self.finished_at:
            return int((self.finished_at - self.started_at).total_seconds() * 1000)
        return None


class Finding(models.Model):
    class Category(models.TextChoices):
        BUG = "bug", "Bug"
        SECURITY = "security", "Security"
        PERFORMANCE = "performance", "Performance"
        MAINTAINABILITY = "maintainability", "Maintainability"
        STYLE = "style", "Style"
        TEST = "test", "Test"

    class PostStatus(models.TextChoices):
        POSTED = "posted", "Posted inline"
        IN_SUMMARY = "in_summary", "Listed in summary"
        BELOW_THRESHOLD = "below_threshold", "Below severity threshold"
        LOW_CONFIDENCE = "low_confidence", "Low confidence"
        DUPLICATE = "duplicate", "Already posted on this PR"
        CAP_EXCEEDED = "cap_exceeded", "Inline comment cap reached"
        NOT_POSTED = "not_posted", "Not posted (review failed)"
        CATEGORY_FILTERED = "category_filtered", "Category excluded by profile"
        DISMISSED_EARLIER = "dismissed_earlier", "Dismissed on an earlier run"
        MERGED = "merged", "Same issue reported twice in this review"
        CONSOLIDATED = "consolidated", "Repeated pattern (grouped in the summary)"

    class State(models.TextChoices):
        OPEN = "open", "Open"
        ACCEPTED = "accepted", "Accepted"
        DISMISSED = "dismissed", "Dismissed"

    class DismissReason(models.TextChoices):
        FALSE_POSITIVE = "false_positive", "False positive"
        WONT_FIX = "wont_fix", "Won't fix"
        DUPLICATE = "duplicate", "Duplicate"
        OTHER = "other", "Other"

    review_run = models.ForeignKey(ReviewRun, on_delete=models.CASCADE, related_name="findings")
    fingerprint = models.CharField(max_length=64, db_index=True)
    path = models.CharField(max_length=1000)
    line_start = models.PositiveIntegerField(null=True, blank=True)
    line_end = models.PositiveIntegerField(null=True, blank=True)
    anchored = models.BooleanField(
        default=False, help_text="Line is part of the diff and can be commented on."
    )
    category = models.CharField(max_length=32, choices=Category.choices)
    severity = models.CharField(max_length=16, choices=Severity.choices)
    confidence = models.FloatField(default=1.0)
    title = models.CharField(max_length=300)
    body = models.TextField()
    suggestion = models.TextField(blank=True)
    rule_id = models.CharField(max_length=64, blank=True)
    post_status = models.CharField(max_length=24, choices=PostStatus.choices, default=PostStatus.NOT_POSTED)
    provider_comment_id = models.CharField(max_length=64, blank=True)
    provider_comment_url = models.URLField(max_length=500, blank=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.OPEN, db_index=True)
    dismiss_reason = models.CharField(max_length=16, choices=DismissReason.choices, blank=True)
    state_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    state_changed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["path", "line_start", "id"]

    def __str__(self) -> str:
        return f"{self.severity}:{self.path}:{self.line_start}"

    @property
    def audit_label(self) -> str:
        return f"{self.title} ({self.path})"


class FindingFeedback(models.Model):
    """A reviewer's thumbs up/down on a finding (RE-16). One vote per user and finding."""

    class Vote(models.TextChoices):
        UP = "up", "Helpful"
        DOWN = "down", "Not helpful"

    finding = models.ForeignKey(Finding, on_delete=models.CASCADE, related_name="feedback")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    vote = models.CharField(max_length=8, choices=Vote.choices)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["finding", "user"], name="uniq_feedback_per_user")]

    def __str__(self) -> str:
        return f"{self.vote}:{self.finding_id}"


class LLMUsage(models.Model):
    class Status(models.TextChoices):
        OK = "ok", "OK"
        ERROR = "error", "Error"

    review_run = models.ForeignKey(
        ReviewRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="usage"
    )
    credential = models.ForeignKey(
        "credentials.LLMCredential", null=True, on_delete=models.SET_NULL, related_name="usage"
    )
    repository = models.ForeignKey(
        "repositories.Repository", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    provider = models.CharField(max_length=32)
    model = models.CharField(max_length=200)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    latency_ms = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=8, choices=Status.choices)
    error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["credential", "created_at"]),
            models.Index(fields=["repository", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"usage:{self.model}:{self.input_tokens}/{self.output_tokens}"
