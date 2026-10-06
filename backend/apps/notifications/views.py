from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db.models import QuerySet
from drf_spectacular.utils import extend_schema
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.accounts.permissions import IsAdmin
from apps.audit.services import record
from apps.notifications import senders, services
from apps.notifications.messages import Message
from apps.notifications.models import Event, NotificationChannel
from apps.notifications.serializers import NotificationChannelSerializer, NotificationDeliverySerializer


def _audit_fields(channel: NotificationChannel) -> dict[str, Any]:
    return {
        "kind": channel.kind,
        "enabled": channel.enabled,
        "events": list(channel.events),
        "min_risk": channel.min_risk,
        "min_severity": channel.min_severity,
        "url": channel.url_hint,
        "recipients": len(channel.recipients),
    }


class NotificationChannelViewSet(viewsets.ModelViewSet[NotificationChannel]):
    """Slack, email and webhook notification channels (admin only)."""

    serializer_class = NotificationChannelSerializer
    permission_classes = [IsAdmin]
    pagination_class = None

    def get_queryset(self) -> QuerySet[NotificationChannel]:
        return NotificationChannel.objects.prefetch_related("repositories")

    def perform_create(self, serializer: Any) -> None:
        channel = serializer.save(created_by=self.request.user)
        record("notification.channel_created", request=self.request, target=channel, **_audit_fields(channel))

    def perform_update(self, serializer: Any) -> None:
        before = _audit_fields(serializer.instance)
        channel = serializer.save()
        after = _audit_fields(channel)
        changes = {k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]}
        if changes or "secret" in serializer.validated_data:
            record("notification.channel_updated", request=self.request, target=channel, changes=changes)

    def perform_destroy(self, instance: NotificationChannel) -> None:
        record("notification.channel_deleted", request=self.request, target=instance)
        instance.delete()

    @extend_schema(request=None, responses={200: {"type": "object"}})
    @action(detail=True, methods=["post"])
    def test(self, request: Request, pk: str | None = None) -> Response:
        """Sends a test message right away and reports the result."""
        channel = self.get_object()
        message = Message(
            event="test",
            title="Reviewbot test notification",
            text=f'This is a test of the "{channel.name}" channel. Notifications will look like this.',
            url=f"{settings.PUBLIC_URL}/settings/notifications",
            fields=[("Channel", channel.name), ("Events", ", ".join(channel.events))],
            data={"test": True},
        )
        try:
            senders.send(channel, message)
        except senders.DeliveryError as exc:
            return Response({"ok": False, "error": str(exc)})
        return Response({"ok": True, "error": ""})

    @extend_schema(responses={200: NotificationDeliverySerializer(many=True)})
    @action(detail=True, methods=["get"])
    def deliveries(self, request: Request, pk: str | None = None) -> Response:
        channel = self.get_object()
        rows = channel.deliveries.all()[:50]
        return Response(NotificationDeliverySerializer(rows, many=True).data)

    @extend_schema(request=None, responses={200: {"type": "object"}})
    @action(detail=False, methods=["get"], url_path="digest-preview")
    def digest_preview(self, request: Request) -> Response:
        """The weekly digest as it would be sent now (not sent)."""
        return Response(services.build_digest().as_dict())

    @extend_schema(responses={200: {"type": "array"}})
    @action(detail=False, methods=["get"])
    def events(self, request: Request) -> Response:
        return Response([{"id": e.value, "label": e.label} for e in Event])
