from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.reviews.engine.profiles import DEFAULT_PROFILE
from apps.reviews.models import Finding, FindingFeedback, PullRequest, ReviewRun

SEVERITIES = ["critical", "high", "medium", "low", "info"]
# Findings that count as "reported" for the PR (excludes ones filtered by threshold/confidence).
REPORTED_STATUSES = [
    Finding.PostStatus.POSTED,
    Finding.PostStatus.IN_SUMMARY,
    Finding.PostStatus.CAP_EXCEEDED,
    Finding.PostStatus.DUPLICATE,
    Finding.PostStatus.NOT_POSTED,
]


def severity_counts(run: ReviewRun) -> dict[str, int]:
    annotated = {s: getattr(run, f"count_{s}", None) for s in SEVERITIES}
    if all(v is not None for v in annotated.values()):
        return {s: int(v or 0) for s, v in annotated.items()}
    counts = dict.fromkeys(SEVERITIES, 0)
    for f in run.findings.all():
        if f.post_status in REPORTED_STATUSES:
            counts[f.severity] += 1
    return counts


class ReviewRunSummarySerializer(serializers.ModelSerializer[ReviewRun]):
    counts = serializers.SerializerMethodField()
    posted_count = serializers.SerializerMethodField()

    class Meta:
        model = ReviewRun
        fields = [
            "id",
            "status",
            "status_reason",
            "trigger",
            "head_sha",
            "created_at",
            "finished_at",
            "counts",
            "posted_count",
            "risk_score",
        ]

    def get_counts(self, obj: ReviewRun) -> dict[str, int]:
        return severity_counts(obj)

    def get_posted_count(self, obj: ReviewRun) -> int:
        value = getattr(obj, "count_posted", None)
        if value is not None:
            return int(value)
        return sum(1 for f in obj.findings.all() if f.post_status == Finding.PostStatus.POSTED)


class PullRequestSerializer(serializers.ModelSerializer[PullRequest]):
    repository = serializers.SerializerMethodField()
    latest_review = serializers.SerializerMethodField()

    class Meta:
        model = PullRequest
        fields = [
            "id",
            "repository",
            "number",
            "title",
            "author_login",
            "state",
            "is_draft",
            "html_url",
            "head_sha",
            "updated_at",
            "reviews_paused",
            "latest_review",
        ]

    def get_repository(self, obj: PullRequest) -> dict[str, Any]:
        return {"id": obj.repository_id, "full_name": obj.repository.full_name}

    def get_latest_review(self, obj: PullRequest) -> dict[str, Any] | None:
        latest: dict[int, ReviewRun] = self.context.get("latest_runs", {})
        run = latest.get(obj.pk)
        if run is None and "latest_runs" not in self.context:
            run = obj.review_runs.order_by("-created_at", "-id").first()
        return ReviewRunSummarySerializer(run).data if run else None


class FindingSerializer(serializers.ModelSerializer[Finding]):
    post_status_label = serializers.CharField(source="get_post_status_display", read_only=True)
    votes = serializers.SerializerMethodField()

    class Meta:
        model = Finding
        fields = [
            "id",
            "path",
            "line_start",
            "line_end",
            "anchored",
            "category",
            "severity",
            "confidence",
            "title",
            "body",
            "suggestion",
            "post_status",
            "post_status_label",
            "provider_comment_url",
            "fingerprint",
            "rule_id",
            "state",
            "dismiss_reason",
            "state_changed_at",
            "votes",
        ]

    def get_votes(self, obj: Finding) -> dict[str, Any]:
        request = self.context.get("request")
        user_id = getattr(getattr(request, "user", None), "pk", None)
        feedback = list(obj.feedback.all())
        mine = next((f.vote for f in feedback if f.user_id == user_id), None)
        return {
            "up": sum(1 for f in feedback if f.vote == FindingFeedback.Vote.UP),
            "down": sum(1 for f in feedback if f.vote == FindingFeedback.Vote.DOWN),
            "mine": mine,
        }


class FindingStateSerializer(serializers.Serializer[Any]):
    state = serializers.ChoiceField(choices=Finding.State.choices)
    dismiss_reason = serializers.ChoiceField(choices=Finding.DismissReason.choices, required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs["state"] == Finding.State.DISMISSED:
            attrs.setdefault("dismiss_reason", Finding.DismissReason.OTHER)
        else:
            attrs["dismiss_reason"] = ""
        return attrs


class FeedbackVoteSerializer(serializers.Serializer[Any]):
    vote = serializers.ChoiceField(choices=FindingFeedback.Vote.choices)


class ReviewRunSerializer(ReviewRunSummarySerializer):
    pull_request = PullRequestSerializer(read_only=True)
    stage_label = serializers.CharField(source="get_stage_display", read_only=True)
    credential_name = serializers.CharField(source="credential.name", default=None, read_only=True)
    created_by_email = serializers.CharField(source="created_by.email", default=None, read_only=True)
    duration_ms = serializers.IntegerField(read_only=True)
    findings = FindingSerializer(many=True, read_only=True)
    profile = serializers.SerializerMethodField()

    class Meta(ReviewRunSummarySerializer.Meta):
        fields = [
            *ReviewRunSummarySerializer.Meta.fields,
            "pull_request",
            "stage",
            "stage_label",
            "error",
            "base_sha",
            "model",
            "credential_name",
            "summary",
            "input_tokens",
            "output_tokens",
            "chunk_count",
            "chunks_failed",
            "duration_ms",
            "started_at",
            "provider_review_url",
            "files_reviewed",
            "files_ignored",
            "findings",
            "created_by_email",
            "incremental",
            "compare_base_sha",
            "profile",
            "config_source",
            "config_error",
        ]

    def get_profile(self, obj: ReviewRun) -> str:
        return str(obj.settings_snapshot.get("profile") or DEFAULT_PROFILE)
