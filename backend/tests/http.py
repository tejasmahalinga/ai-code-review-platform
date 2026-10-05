"""Tiny HTTP mocking helper built on ``httpx2.MockTransport`` (no network in tests)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2

Handler = Callable[[httpx2.Request], httpx2.Response]


@dataclass
class Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler
    calls: list[httpx2.Request] = field(default_factory=list)


class MockRouter:
    def __init__(self) -> None:
        self.routes: list[Route] = []
        self.requests: list[httpx2.Request] = []

    def add(
        self,
        method: str,
        url_regex: str,
        response: Any = None,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        handler: Handler | None = None,
    ) -> Route:
        if handler is None:

            def handler(
                _request: httpx2.Request, _body=response, _status=status, _headers=headers
            ) -> httpx2.Response:
                if isinstance(_body, str | bytes):
                    return httpx2.Response(_status, content=_body, headers=_headers)
                return httpx2.Response(_status, json=_body, headers=_headers)

        route = Route(method.upper(), re.compile(url_regex), handler)
        self.routes.append(route)
        return route

    def _dispatch(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        for route in self.routes:
            if route.method == request.method and route.pattern.search(str(request.url)):
                route.calls.append(request)
                return route.handler(request)
        raise AssertionError(f"Unmocked request: {request.method} {request.url}")

    def client(self, **kwargs: Any) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self._dispatch), **kwargs)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._dispatch)


def body(request: httpx2.Request) -> Any:
    return json.loads(request.content.decode())
