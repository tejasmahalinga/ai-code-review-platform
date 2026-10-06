from __future__ import annotations

from typing import Any

from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle


class LoginThrottle(SimpleRateThrottle):
    """Limits login attempts per (client IP, email) pair. Only failed attempts are counted."""

    scope = "login"
    num_requests: int
    duration: int

    def get_cache_key(self, request: Request, view: Any) -> str:
        data = request.data if isinstance(request.data, dict) else {}
        email = str(data.get("email", "")).strip().lower()
        return self.cache_format % {"scope": self.scope, "ident": f"{self.get_ident(request)}:{email}"}

    def allow_request(self, request: Request, view: Any) -> bool:
        # Peek without recording; failed logins are recorded explicitly via ``record_failure``.
        if self.rate is None:
            return True
        self.key = self.get_cache_key(request, view)
        self.history = self.cache.get(self.key, [])
        self.now = self.timer()
        while self.history and self.history[-1] <= self.now - self.duration:
            self.history.pop()
        if len(self.history) >= self.num_requests:
            return self.throttle_failure()
        return True

    def record_failure(self, request: Request, view: Any) -> None:
        self.key = self.get_cache_key(request, view)
        history = self.cache.get(self.key, [])
        now = self.timer()
        history = [t for t in history if t > now - self.duration]
        history.insert(0, now)
        self.cache.set(self.key, history, self.duration)


class SetupThrottle(SimpleRateThrottle):
    scope = "setup"

    def get_cache_key(self, request: Request, view: Any) -> str:
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class InviteThrottle(SimpleRateThrottle):
    """Limits invite lookups and acceptance per client IP (tokens are unguessable; this stops scanning)."""

    scope = "invite"

    def get_cache_key(self, request: Request, view: Any) -> str:
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}
