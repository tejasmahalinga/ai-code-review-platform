from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.notifications.models import Event, NotificationChannel, NotificationDelivery
from apps.notifications.urlguard import UnsafeURL, check_url
from apps.repositories.models import Repository, Severity


class NotificationChannelSerializer(serializers.ModelSerializer[NotificationChannel]):
    url = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=1000)
    secret = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=200)
    has_secret = serializers.SerializerMethodField()
    events = serializers.ListField(child=serializers.ChoiceField(choices=Event.choices), min_length=1)
    recipients = serializers.ListField(child=serializers.EmailField(), required=False, max_length=50)
    min_severity = serializers.ChoiceField(choices=[("", "none"), *Severity.choices], required=False)
    min_risk = serializers.IntegerField(min_value=0, max_value=100, required=False)
    repositories = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Repository.objects.all(), required=False
    )

    class Meta:
        model = NotificationChannel
        fields = [
            "id",
            "name",
            "kind",
            "enabled",
            "url",
            "url_hint",
            "secret",
            "has_secret",
            "recipients",
            "events",
            "min_risk",
            "min_severity",
            "repositories",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["url_hint", "created_at", "updated_at"]

    def get_has_secret(self, obj: NotificationChannel) -> bool:
        return bool(obj.encrypted_secret)

    def validate_kind(self, value: str) -> str:
        if self.instance is not None and value != self.instance.kind:
            raise serializers.ValidationError("The kind cannot be changed; create a new channel instead.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        kind = attrs.get("kind") or (self.instance.kind if self.instance else None)
        if kind in (NotificationChannel.Kind.SLACK, NotificationChannel.Kind.WEBHOOK):
            url = attrs.get("url")
            if url is None and (self.instance is None or not self.instance.encrypted_url):
                raise serializers.ValidationError({"url": "This field is required."})
            if url is not None:
                try:
                    check_url(url.strip(), slack=kind == NotificationChannel.Kind.SLACK)
                except UnsafeURL as exc:
                    raise serializers.ValidationError({"url": str(exc)}) from exc
        if kind == NotificationChannel.Kind.EMAIL:
            recipients = attrs.get("recipients", self.instance.recipients if self.instance else [])
            if not recipients:
                raise serializers.ValidationError({"recipients": "Add at least one email address."})
        return attrs

    def _apply_secrets(self, channel: NotificationChannel, url: str | None, secret: str | None) -> None:
        if url is not None:
            channel.set_url(url.strip())
        if secret is not None:
            channel.set_secret(secret)

    def create(self, validated_data: dict[str, Any]) -> NotificationChannel:
        url, secret = validated_data.pop("url", None), validated_data.pop("secret", None)
        repositories = validated_data.pop("repositories", [])
        channel = NotificationChannel(**validated_data)
        self._apply_secrets(channel, url, secret)
        channel.save()
        channel.repositories.set(repositories)
        return channel

    def update(self, instance: NotificationChannel, validated_data: dict[str, Any]) -> NotificationChannel:
        url, secret = validated_data.pop("url", None), validated_data.pop("secret", None)
        self._apply_secrets(instance, url, secret)
        return super().update(instance, validated_data)


class NotificationDeliverySerializer(serializers.ModelSerializer[NotificationDelivery]):
    class Meta:
        model = NotificationDelivery
        fields = ["id", "event", "title", "status", "error", "attempts", "created_at", "sent_at"]
        read_only_fields = fields
