"""Recording audit events. Callers pass the request (Django or DRF) for actor, IP and user agent."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import models

from apps.audit.models import AuditEvent
from apps.core.logging import get_logger, redact

logger = get_logger(__name__)


def client_ip(request: Any) -> str | None:
    if request is None:
        return None
    if settings.BEHIND_PROXY:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def _target_fields(target: models.Model | None) -> dict[str, str]:
    if target is None:
        return {}
    label = getattr(target, "audit_label", None) or str(target)
    return {
        "target_type": target._meta.model_name or "",
        "target_id": str(target.pk),
        "target_label": str(label)[:300],
    }


def record(
    action: str,
    *,
    request: Any = None,
    actor: Any = None,
    target: models.Model | None = None,
    **metadata: Any,
) -> AuditEvent:
    """Store an audit event. ``metadata`` is redacted the same way as logs: no secrets are ever stored."""
    if actor is None and request is not None:
        user = getattr(request, "user", None)
        actor = user if user is not None and user.is_authenticated else None
    ip = client_ip(request)
    event = AuditEvent(
        action=action,
        actor=actor,
        actor_email=getattr(actor, "email", "") or "",
        ip=ip,
        user_agent=(request.META.get("HTTP_USER_AGENT", "") if request is not None else "")[:300],
        metadata=redact(metadata) if metadata else {},
        **_target_fields(target),
    )
    try:
        event.save()
    except ValueError:  # malformed IP from a proxy header
        event.ip = None
        event.save()
    logger.info("audit.recorded", action=action, target_type=event.target_type, target_id=event.target_id)
    return event


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Changed keys as ``{key: {"from": old, "to": new}}``."""
    return {
        key: {"from": before.get(key), "to": after.get(key)}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }
