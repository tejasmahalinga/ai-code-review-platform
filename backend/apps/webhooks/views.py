from __future__ import annotations

import json
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from rest_framework import mixins, serializers, viewsets

from apps.accounts.permissions import IsAdmin
from apps.core.logging import get_logger
from apps.git_providers.github.app import verify_signature
from apps.repositories.services import github_connection
from apps.webhooks import github_handlers
from apps.webhooks.models import WebhookDelivery

logger = get_logger(__name__)

MAX_BODY_BYTES = 25 * 1024 * 1024  # GitHub caps payloads at 25 MB.


@csrf_exempt
@require_POST
def github_webhook(request: HttpRequest) -> JsonResponse:
    connection = github_connection()
    if connection is None:
        return JsonResponse({"error": "GitHub is not configured"}, status=404)
    body = request.body
    if len(body) > MAX_BODY_BYTES:
        return JsonResponse({"error": "payload too large"}, status=413)
    if not verify_signature(connection.webhook_secret, body, request.headers.get("X-Hub-Signature-256")):
        logger.warning("webhook.invalid_signature", provider="github")
        return JsonResponse({"error": "invalid signature"}, status=401)

    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    event = request.headers.get("X-GitHub-Event", "")
    if not delivery_id or not event:
        return JsonResponse({"error": "missing delivery headers"}, status=400)
    try:
        payload: dict[str, Any] = json.loads(body)
    except ValueError:
        return JsonResponse({"error": "invalid JSON"}, status=400)

    with transaction.atomic():
        try:
            with transaction.atomic():
                delivery = WebhookDelivery.objects.create(
                    provider="github",
                    delivery_id=delivery_id,
                    event=event,
                    action=str(payload.get("action", ""))[:64],
                    payload=payload,
                )
        except IntegrityError:
            delivery = WebhookDelivery.objects.select_for_update().get(
                provider="github", delivery_id=delivery_id
            )
            if delivery.status != WebhookDelivery.Status.FAILED:
                return JsonResponse({"status": "duplicate"}, status=200)
        try:
            with transaction.atomic():
                status, reason, repository, run = github_handlers.handle(connection, event, payload)
        except Exception:
            logger.exception("webhook.handler_failed", delivery_id=delivery_id, gh_event=event)
            delivery.status = WebhookDelivery.Status.FAILED
            delivery.reason = "handler_error"
            delivery.save(update_fields=["status", "reason"])
            return JsonResponse({"status": "failed"}, status=500)
        delivery.status = status
        delivery.reason = reason[:300]
        delivery.repository = repository
        delivery.review_run = run
        delivery.save(update_fields=["status", "reason", "repository", "review_run"])
    logger.info("webhook.received", delivery_id=delivery_id, gh_event=event, status=status, reason=reason)
    return JsonResponse({"status": status, "reason": reason}, status=202)


class WebhookDeliverySerializer(serializers.ModelSerializer[WebhookDelivery]):
    repository = serializers.CharField(source="repository.full_name", default=None, read_only=True)

    class Meta:
        model = WebhookDelivery
        fields = [
            "id",
            "provider",
            "delivery_id",
            "event",
            "action",
            "repository",
            "status",
            "reason",
            "review_run",
            "received_at",
        ]


class WebhookDeliveryViewSet(mixins.ListModelMixin, viewsets.GenericViewSet[WebhookDelivery]):
    serializer_class = WebhookDeliverySerializer
    permission_classes = [IsAdmin]

    def get_queryset(self) -> QuerySet[WebhookDelivery]:
        qs = WebhookDelivery.objects.select_related("repository")
        if status := self.request.query_params.get("status"):
            qs = qs.filter(status=status)
        return qs
