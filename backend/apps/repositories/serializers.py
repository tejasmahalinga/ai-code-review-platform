from __future__ import annotations

from typing import Any

import pathspec
from rest_framework import serializers

from apps.credentials.models import LLMCredential
from apps.repositories.models import Installation, Repository, RepositorySettings
from apps.reviews.engine.ignore import DEFAULT_IGNORE_PATTERNS
from apps.reviews.engine.repo_config import ConfigError, normalize_rules

# Settings a reviewer may change: the same review-content knobs a `.reviewbot.yml` can set, plus
# test suggestions. Keys, cost guards, triggers and the check gate stay admin-only.
REVIEWER_SETTINGS_FIELDS = frozenset(
    {
        "profile",
        "min_severity",
        "min_confidence",
        "max_inline_comments",
        "ignore_patterns",
        "replace_default_ignores",
        "custom_instructions",
        "rules",
        "suggest_tests",
        "post_when_no_findings",
    }
)


class InstallationSerializer(serializers.ModelSerializer[Installation]):
    suspended = serializers.SerializerMethodField()
    repository_count = serializers.SerializerMethodField()

    class Meta:
        model = Installation
        fields = ["id", "account_login", "account_type", "suspended", "repository_count"]

    def get_suspended(self, obj: Installation) -> bool:
        return obj.suspended_at is not None

    def get_repository_count(self, obj: Installation) -> int:
        return obj.repositories.filter(status=Repository.Status.ACTIVE).count()


class RepositorySerializer(serializers.ModelSerializer[Repository]):
    installation_account = serializers.CharField(source="installation.account_login", read_only=True)
    provider = serializers.CharField(source="installation.connection.provider", read_only=True)
    webhook_managed = serializers.SerializerMethodField()
    credential_name = serializers.SerializerMethodField()
    model = serializers.SerializerMethodField()
    auto_review = serializers.SerializerMethodField()
    pull_request_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Repository
        fields = [
            "id",
            "full_name",
            "private",
            "html_url",
            "default_branch",
            "status",
            "enabled",
            "installation_account",
            "provider",
            "webhook_managed",
            "credential_name",
            "model",
            "auto_review",
            "pull_request_count",
        ]
        read_only_fields = [f for f in fields if f != "enabled"]

    def get_webhook_managed(self, obj: Repository) -> bool | None:
        """GitLab only: whether Reviewbot created the project webhook (None for GitHub, which needs none)."""
        if obj.installation.connection.provider != "gitlab":
            return None
        return bool(obj.webhook_id)

    def get_credential_name(self, obj: Repository) -> str | None:
        settings = getattr(obj, "settings", None)
        return settings.credential.name if settings and settings.credential else None

    def get_model(self, obj: Repository) -> str:
        settings = getattr(obj, "settings", None)
        return settings.effective_model if settings else ""

    def get_auto_review(self, obj: Repository) -> bool:
        settings = getattr(obj, "settings", None)
        return bool(settings and settings.auto_review)

    def validate_enabled(self, value: bool) -> bool:
        repository: Repository | None = self.instance
        if value and repository is not None:
            if repository.status != Repository.Status.ACTIVE:
                raise serializers.ValidationError(
                    "This repository was removed from the GitHub App installation."
                )
            settings = getattr(repository, "settings", None)
            if not (settings and settings.credential and settings.credential.is_usable):
                raise serializers.ValidationError(
                    "Select a valid LLM key in the repository settings before enabling reviews."
                )
        return value


class RepositorySettingsSerializer(serializers.ModelSerializer[RepositorySettings]):
    effective_model = serializers.CharField(read_only=True)
    default_ignore_patterns = serializers.SerializerMethodField()
    credential = serializers.PrimaryKeyRelatedField(
        queryset=LLMCredential.objects.exclude(status=LLMCredential.Status.REVOKED), allow_null=True
    )
    ignore_patterns = serializers.ListField(
        child=serializers.CharField(max_length=300, trim_whitespace=True, allow_blank=True),
        max_length=200,
        required=False,
    )
    base_branch_patterns = serializers.ListField(
        child=serializers.CharField(max_length=255, allow_blank=True), max_length=50, required=False
    )
    rules = serializers.JSONField(required=False)
    custom_instructions = serializers.CharField(max_length=4000, allow_blank=True, required=False)
    min_confidence = serializers.FloatField(min_value=0.0, max_value=1.0, required=False)
    max_inline_comments = serializers.IntegerField(min_value=0, max_value=100, required=False)
    max_changed_lines = serializers.IntegerField(min_value=1, max_value=100_000, required=False)
    max_files = serializers.IntegerField(min_value=1, max_value=3000, required=False)
    max_input_tokens = serializers.IntegerField(min_value=1_000, max_value=5_000_000, required=False)
    chunk_tokens = serializers.IntegerField(min_value=1_000, max_value=500_000, required=False)

    class Meta:
        model = RepositorySettings
        fields = [
            "auto_review",
            "review_drafts",
            "credential",
            "model",
            "effective_model",
            "ignore_patterns",
            "replace_default_ignores",
            "default_ignore_patterns",
            "custom_instructions",
            "min_severity",
            "min_confidence",
            "max_inline_comments",
            "max_changed_lines",
            "max_files",
            "max_input_tokens",
            "chunk_tokens",
            "post_when_no_findings",
            "profile",
            "review_on_push",
            "check_runs",
            "gate_severity",
            "base_branch_patterns",
            "rules",
            "suggest_tests",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]

    def get_default_ignore_patterns(self, obj: RepositorySettings) -> list[str]:
        return list(DEFAULT_IGNORE_PATTERNS)

    def validate_ignore_patterns(self, value: list[str]) -> list[str]:
        cleaned = [p for p in (v.strip() for v in value) if p and not p.startswith("#")]
        try:
            pathspec.GitIgnoreSpec.from_lines(cleaned)
        except Exception as exc:
            raise serializers.ValidationError(f"Invalid pattern: {exc}") from exc
        return cleaned

    def validate_base_branch_patterns(self, value: list[str]) -> list[str]:
        return [p for p in (v.strip() for v in value) if p]

    def validate_rules(self, value: Any) -> list[dict[str, Any]]:
        try:
            return normalize_rules(value)
        except ConfigError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        instance: RepositorySettings | None = self.instance
        if instance is not None and instance.repository.enabled and "credential" in attrs:
            credential = attrs["credential"]
            if credential is None or not credential.is_usable:
                raise serializers.ValidationError(
                    {"credential": "An enabled repository needs a valid LLM key. Disable reviews first."}
                )
        if "credential" in attrs and attrs["credential"] is not None and not attrs["credential"].is_usable:
            raise serializers.ValidationError({"credential": "This LLM key is not valid."})
        return attrs
