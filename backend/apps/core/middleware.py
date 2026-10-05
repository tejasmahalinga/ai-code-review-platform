from __future__ import annotations

import re
import uuid
from collections.abc import Callable

import structlog
from django.http import HttpRequest, HttpResponse

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9\-_.]{1,64}$")


class RequestIdMiddleware:
    """Binds a request id to the logging context and echoes it in ``X-Request-ID``."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        incoming = request.headers.get("X-Request-ID", "")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response = self.get_response(request)
        response["X-Request-ID"] = request_id
        return response
