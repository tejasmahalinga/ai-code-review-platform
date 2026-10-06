"""Role-based permissions (ADM-02).

| Capability                                                        | admin | reviewer | viewer |
|-------------------------------------------------------------------|-------|----------|--------|
| View pull requests, reviews, findings, repositories, settings     | yes   | yes      | yes    |
| Re-run reviews, accept/dismiss/vote on findings                   | yes   | yes      | no     |
| Edit review rules on a repository (REVIEWER_SETTINGS_FIELDS)      | yes   | yes      | no     |
| Enable repos, keys, cost guards, integrations, users, audit, usage| yes   | no       | no     |
"""

from __future__ import annotations

from typing import Any

from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request


def _authenticated(request: Request) -> bool:
    user = request.user
    return bool(user and user.is_authenticated)


class IsAdmin(BasePermission):
    message = "Admin role required."

    def has_permission(self, request: Request, view: Any) -> bool:
        return _authenticated(request) and getattr(request.user, "is_admin", False)


class IsAdminOrReadOnly(BasePermission):
    message = "Admin role required."

    def has_permission(self, request: Request, view: Any) -> bool:
        if not _authenticated(request):
            return False
        return request.method in SAFE_METHODS or getattr(request.user, "is_admin", False)


class IsReviewerOrReadOnly(BasePermission):
    message = "Reviewer or admin role required."

    def has_permission(self, request: Request, view: Any) -> bool:
        if not _authenticated(request):
            return False
        return request.method in SAFE_METHODS or getattr(request.user, "can_review", False)


class SessionOnly(BasePermission):
    """Blocks API-token requests, so a leaked token cannot mint more tokens or change the password."""

    message = "Sign in to the dashboard to do this; API tokens cannot."

    def has_permission(self, request: Request, view: Any) -> bool:
        return _authenticated(request) and request.auth is None
