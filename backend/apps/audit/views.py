from __future__ import annotations

from datetime import datetime

from django.db.models import QuerySet
from django.utils.dateparse import parse_datetime
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import ValidationError

from apps.accounts.permissions import IsAdmin
from apps.audit.models import AuditEvent


class AuditEventSerializer(serializers.ModelSerializer[AuditEvent]):
    class Meta:
        model = AuditEvent
        fields = [
            "id",
            "created_at",
            "actor",
            "actor_email",
            "action",
            "target_type",
            "target_id",
            "target_label",
            "ip",
            "user_agent",
            "metadata",
        ]
        read_only_fields = fields


def _parse_time(name: str, value: str) -> datetime:
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValidationError({name: "Use an ISO 8601 timestamp."})
    return parsed


class AuditEventViewSet(mixins.ListModelMixin, viewsets.GenericViewSet[AuditEvent]):
    """Read-only audit log. There is deliberately no update or delete endpoint."""

    serializer_class = AuditEventSerializer
    permission_classes = [IsAdmin]

    def get_queryset(self) -> QuerySet[AuditEvent]:
        qs = AuditEvent.objects.all()
        params = self.request.query_params
        if action := params.get("action"):
            # "credential" matches every credential.* event; "credential.created" matches exactly.
            qs = qs.filter(action=action) if "." in action else qs.filter(action__startswith=f"{action}.")
        if actor := params.get("actor"):
            qs = qs.filter(actor_email__icontains=actor)
        if target_type := params.get("target_type"):
            qs = qs.filter(target_type=target_type)
        if target_id := params.get("target_id"):
            qs = qs.filter(target_id=target_id)
        if since := params.get("since"):
            qs = qs.filter(created_at__gte=_parse_time("since", since))
        if until := params.get("until"):
            qs = qs.filter(created_at__lt=_parse_time("until", until))
        return qs
