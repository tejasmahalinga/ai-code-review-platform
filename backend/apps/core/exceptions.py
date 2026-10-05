"""Uniform API error envelope: ``{"error": {"code", "message", "details"}}``."""

from __future__ import annotations

from typing import Any

from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The request conflicts with the current state of the resource."
    default_code = "conflict"


def _first_message(detail: Any) -> str:
    if isinstance(detail, list) and detail:
        return _first_message(detail[0])
    if isinstance(detail, dict) and detail:
        key, value = next(iter(detail.items()))
        message = _first_message(value)
        return message if key in ("non_field_errors", "detail") else f"{key}: {message}"
    return str(detail)


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    response = drf_exception_handler(exc, context)
    if response is None:
        return None
    body: dict[str, Any]
    if isinstance(exc, ValidationError):
        body = {
            "error": {
                "code": "validation_error",
                "message": _first_message(exc.detail),
                "details": exc.detail,
            }
        }
    else:
        detail = getattr(exc, "detail", str(exc))
        code = detail.code if hasattr(detail, "code") else getattr(exc, "default_code", "error")
        body = {"error": {"code": code, "message": str(detail), "details": None}}
    response.data = body
    return response
