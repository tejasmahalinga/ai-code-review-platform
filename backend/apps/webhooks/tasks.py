from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.webhooks.models import WebhookDelivery


@shared_task
def prune_deliveries() -> int:
    cutoff = timezone.now() - timedelta(days=settings.WEBHOOK_RETENTION_DAYS)
    deleted, _ = WebhookDelivery.objects.filter(received_at__lt=cutoff).delete()
    return deleted
