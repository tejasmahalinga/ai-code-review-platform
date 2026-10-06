from __future__ import annotations

from typing import Any

from celery import shared_task
from django.utils import timezone

from apps.core.logging import get_logger
from apps.notifications import senders, services
from apps.notifications.messages import Message
from apps.notifications.models import NotificationDelivery

logger = get_logger(__name__)

MAX_ATTEMPTS = 4


@shared_task(bind=True, max_retries=MAX_ATTEMPTS - 1)
def deliver(self: Any, delivery_id: int, payload: dict[str, Any]) -> str:
    delivery = NotificationDelivery.objects.select_related("channel").filter(pk=delivery_id).first()
    if delivery is None or delivery.status == NotificationDelivery.Status.SENT:
        return "skipped"
    delivery.attempts += 1
    try:
        senders.send(delivery.channel, Message.from_dict(payload))
    except senders.TransientError as exc:
        delivery.error = str(exc)[:500]
        if self.request.retries < MAX_ATTEMPTS - 1:
            delivery.save(update_fields=["attempts", "error"])
            raise self.retry(countdown=30 * 2**self.request.retries, exc=exc) from exc
        delivery.status = NotificationDelivery.Status.FAILED
    except senders.DeliveryError as exc:
        delivery.status = NotificationDelivery.Status.FAILED
        delivery.error = str(exc)[:500]
    else:
        delivery.status = NotificationDelivery.Status.SENT
        delivery.error = ""
        delivery.sent_at = timezone.now()
    delivery.save(update_fields=["attempts", "error", "status", "sent_at"])
    log = logger.info if delivery.status == NotificationDelivery.Status.SENT else logger.warning
    log("notification.delivered", delivery_id=delivery.pk, status=delivery.status, event=delivery.event)
    return delivery.status


@shared_task
def weekly_digest() -> int:
    return services.send_weekly_digest()
