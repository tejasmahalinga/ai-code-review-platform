from __future__ import annotations

import hmac

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

from apps.core.logging import get_logger

logger = get_logger(__name__)


@require_GET
def healthz(_request: HttpRequest) -> JsonResponse:
    """Liveness: the process is up and serving requests."""
    return JsonResponse({"status": "ok"})


@require_GET
def readyz(_request: HttpRequest) -> JsonResponse:
    """Readiness: database and cache/broker are reachable."""
    checks: dict[str, str] = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception as exc:
        logger.warning("readiness.database_failed", error=str(exc))
        checks["database"] = "error"
    try:
        from django.core.cache import cache

        cache.set("readyz", "1", 5)
        checks["cache"] = "ok" if cache.get("readyz") == "1" else "error"
    except Exception as exc:
        logger.warning("readiness.cache_failed", error=str(exc))
        checks["cache"] = "error"
    ok = all(v == "ok" for v in checks.values())
    return JsonResponse({"status": "ok" if ok else "error", "checks": checks}, status=200 if ok else 503)


@require_GET
def metrics(request: HttpRequest) -> HttpResponse:
    """Prometheus metrics, optionally protected by ``REVIEWBOT_METRICS_TOKEN`` (Bearer)."""
    token = settings.METRICS_TOKEN
    if token:
        provided = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(provided.encode(), token.encode()):
            return HttpResponse(status=401)
    from django_prometheus.exports import ExportToDjangoView

    return ExportToDjangoView(request)
