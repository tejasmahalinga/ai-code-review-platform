from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.audit.models import AuditEvent


@shared_task
def prune_audit_events() -> int:
    cutoff = timezone.now() - timedelta(days=settings.AUDIT_RETENTION_DAYS)
    deleted, _ = AuditEvent.objects.filter(created_at__lt=cutoff).delete()
    return deleted
