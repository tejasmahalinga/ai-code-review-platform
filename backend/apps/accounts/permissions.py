from __future__ import annotations

from typing import Any

from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request


class IsAdmin(BasePermission):
    message = "Admin role required."

    def has_permission(self, request: Request, view: Any) -> bool:
        user = request.user
        return bool(user and user.is_authenticated and getattr(user, "is_admin", False))


class IsAdminOrReadOnly(BasePermission):
    message = "Admin role required."

    def has_permission(self, request: Request, view: Any) -> bool:
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return request.method in SAFE_METHODS or getattr(user, "is_admin", False)
