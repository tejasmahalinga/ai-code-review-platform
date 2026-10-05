from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.credentials.models import LLMCredential
from apps.llm import registry


class LLMCredentialSerializer(serializers.ModelSerializer[LLMCredential]):
    """Never exposes the secret. ``api_key`` is write-only and only accepted on create."""

    api_key = serializers.CharField(
        write_only=True, required=False, allow_blank=True, max_length=4096, trim_whitespace=True
    )
    in_use_by = serializers.SerializerMethodField()

    class Meta:
        model = LLMCredential
        fields = [
            "id",
            "name",
            "provider",
            "base_url",
            "api_key",
            "last4",
            "default_model",
            "status",
            "status_message",
            "last_validated_at",
            "created_at",
            "revoked_at",
            "in_use_by",
        ]
        read_only_fields = [
            "last4",
            "status",
            "status_message",
            "last_validated_at",
            "created_at",
            "revoked_at",
        ]

    def get_in_use_by(self, obj: LLMCredential) -> int:
        return obj.repository_settings.filter(repository__enabled=True).count()

    def validate_provider(self, value: str) -> str:
        if not registry.is_enabled(value):
            raise serializers.ValidationError("Unsupported provider.")
        if self.instance is not None and value != self.instance.provider:
            raise serializers.ValidationError("Provider cannot be changed; create a new credential instead.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        provider = attrs.get("provider") or (self.instance.provider if self.instance else None)
        if provider is None:
            raise serializers.ValidationError({"provider": "This field is required."})
        info, _ = registry.PROVIDERS[provider]
        if self.instance is None:
            if info.requires_api_key and not attrs.get("api_key"):
                raise serializers.ValidationError({"api_key": "This field is required."})
        elif "api_key" in attrs:
            raise serializers.ValidationError({"api_key": "Use the rotate endpoint to change a key."})
        base_url = attrs.get("base_url", self.instance.base_url if self.instance else "")
        if info.requires_base_url and not base_url:
            raise serializers.ValidationError({"base_url": "This provider requires a base URL."})
        return attrs
