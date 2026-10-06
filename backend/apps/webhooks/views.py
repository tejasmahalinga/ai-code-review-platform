from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
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
from apps.repositories.models import GitProviderConnection
from apps.repositories.services import github_connection, gitlab_connection
from apps.webhooks import github_handlers, gitlab_handlers
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
    return _ingest("github", connection, delivery_id, event, body, github_handlers.handle)


@csrf_exempt
@require_POST
def gitlab_webhook(request: HttpRequest) -> JsonResponse:
    connection = gitlab_connection()
    if connection is None:
        return JsonResponse({"error": "GitLab is not configured"}, status=404)
    body = request.body
    if len(body) > MAX_BODY_BYTES:
        return JsonResponse({"error": "payload too large"}, status=413)
    token = request.headers.get("X-Gitlab-Token", "")
    if not token or not hmac.compare_digest(token.encode(), connection.webhook_secret.encode()):
        logger.warning("webhook.invalid_signature", provider="gitlab")
        return JsonResponse({"error": "invalid token"}, status=401)
    event = request.headers.get("X-Gitlab-Event", "")
    # GitLab 14.x+ sends a delivery UUID; newer versions add an Idempotency-Key that is stable across retries.
    delivery_id = (
        request.headers.get("Idempotency-Key")
        or request.headers.get("X-Gitlab-Event-UUID")
        or hashlib.sha256(body).hexdigest()
    )[:100]
    return _ingest("gitlab", connection, delivery_id, event, body, gitlab_handlers.handle)


Handler = Callable[[GitProviderConnection, str, dict[str, Any]], github_handlers.Result]


def _ingest(
    provider: str,
    connection: GitProviderConnection,
    delivery_id: str,
    event: str,
    body: bytes,
    handler: Handler,
) -> JsonResponse:
    try:
        payload: dict[str, Any] = json.loads(body)
    except ValueError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "invalid JSON"}, status=400)
    action = payload.get("action") or (payload.get("object_attributes") or {}).get("action", "")

    with transaction.atomic():
        try:
            with transaction.atomic():
                delivery = WebhookDelivery.objects.create(
                    provider=provider,
                    delivery_id=delivery_id,
                    event=event[:64],
                    action=str(action or "")[:64],
                    payload=payload,
                )
        except IntegrityError:
            delivery = WebhookDelivery.objects.select_for_update().get(
                provider=provider, delivery_id=delivery_id
            )
            if delivery.status != WebhookDelivery.Status.FAILED:
                return JsonResponse({"status": "duplicate"}, status=200)
        try:
            with transaction.atomic():
                status, reason, repository, run = handler(connection, event, payload)
        except Exception:
            logger.exception(
                "webhook.handler_failed", provider=provider, delivery_id=delivery_id, gh_event=event
            )
            delivery.status = WebhookDelivery.Status.FAILED
            delivery.reason = "handler_error"
            delivery.save(update_fields=["status", "reason"])
            return JsonResponse({"status": "failed"}, status=500)
        delivery.status = status
        delivery.reason = reason[:300]
        delivery.repository = repository
        delivery.review_run = run
        delivery.save(update_fields=["status", "reason", "repository", "review_run"])
    logger.info(
        "webhook.received",
        provider=provider,
        delivery_id=delivery_id,
        gh_event=event,
        status=status,
        reason=reason,
    )
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
