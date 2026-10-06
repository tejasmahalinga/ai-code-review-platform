#!/usr/bin/env python3
"""A tiny stand-in for the GitHub REST API, for end-to-end tests (stdlib only).

Serves one installation (id 77) with one repository (acme/demo) and one pull request (#1).
Reviews posted by Reviewbot are recorded and exposed at GET /_posted, check runs at GET /_checks.
POST /_push simulates pushing a new commit (head aaaa... -> cccc...) that adds app/util.py.
Also answers the OAuth web flow for "Sign in with GitHub" as the user dev-octo (dev@example.com).
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PATCH = "\n".join(
    [
        "@@ -0,0 +1,8 @@",
        "+import os",
        "+",
        '+API_KEY = "sk-live-hardcoded-value"',
        "+",
        "+def run(expr):",
        "+    # TODO: validate input",
        "+    return eval(expr)",
        "+",
    ]
)
PR = {
    "number": 1,
    "title": "Add expression runner",
    "user": {"login": "octocat"},
    "state": "open",
    "draft": False,
    "html_url": "https://github.example/acme/demo/pull/1",
    "base": {"ref": "main", "sha": "b" * 40},
    "head": {"ref": "feature", "sha": "a" * 40},
}
FILES = [
    {"filename": "app/runner.py", "status": "added", "additions": 8, "deletions": 0, "patch": PATCH},
    {"filename": "package-lock.json", "status": "modified", "additions": 3, "deletions": 1, "patch": "@@ -1 +1 @@\n-a\n+b"},
]
NEW_HEAD = "c" * 40
UTIL_FILE = {
    "filename": "app/util.py",
    "status": "added",
    "additions": 3,
    "deletions": 0,
    "patch": "@@ -0,0 +1,3 @@\n+def helper():\n+    # TODO: handle errors\n+    return 1",
}
CHECKS: list[dict] = []
CONFIG_FILE = (
    "instructions: Prefer pathlib over os.path.\n"
    "rules:\n"
    "  - id: no-eval\n"
    "    description: Never call eval() on user input\n"
    "    severity: critical\n"
)
REPO = {"id": 4242, "name": "demo", "full_name": "acme/demo", "private": True, "default_branch": "main",
        "html_url": "https://github.example/acme/demo"}
POSTED: list[dict] = []
OAUTH_USER = {"id": 9001, "login": "dev-octo", "name": "Dev Octo"}
OAUTH_EMAILS = [{"email": "dev@example.com", "verified": True, "primary": True}]


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str) -> None:
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:  # quieter logs
        print("fake-github:", fmt % args, flush=True)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        page = re.search(r"[?&]page=(\d+)", self.path)
        first_page = page is None or page.group(1) == "1"
        if path == "/_posted":
            return self._send(200, POSTED)
        if path == "/user":
            return self._send(200, OAUTH_USER)
        if path == "/user/emails":
            return self._send(200, OAUTH_EMAILS)
        if path == "/_checks":
            return self._send(200, CHECKS)
        if path == "/repos/acme/demo/contents/.reviewbot.yml":
            # The config file exists on the base commit only (Reviewbot must never read the PR head's copy).
            if f"ref={'b' * 40}" in self.path:
                return self._send_text(200, CONFIG_FILE)
            return self._send(404, {"message": "Not Found"})
        if path == f"/repos/acme/demo/compare/{'a' * 40}...{NEW_HEAD}":
            return self._send(200, {"status": "ahead", "files": [UTIL_FILE]})
        if path == "/app/installations":
            return self._send(200, [{"id": 77, "account": {"login": "acme", "type": "Organization"}}] if first_page else [])
        if path == "/app/installations/77":
            return self._send(200, {"id": 77, "account": {"login": "acme", "type": "Organization"}})
        if path == "/installation/repositories":
            return self._send(200, {"total_count": 1, "repositories": [REPO] if first_page else []})
        if path == "/repos/acme/demo/pulls/1":
            return self._send(200, PR)
        if path == "/repos/acme/demo/pulls/1/files":
            return self._send(200, FILES if first_page else [])
        match = re.fullmatch(r"/repos/acme/demo/pulls/1/reviews/(\d+)/comments", path)
        if match:
            review = POSTED[int(match.group(1)) - 1]
            return self._send(200, [
                {"id": int(match.group(1)) * 100 + i, "path": c["path"], "line": c["line"],
                 "html_url": f"https://github.example/acme/demo/pull/1#discussion_r{i}"}
                for i, c in enumerate(review["comments"])
            ])
        return self._send(404, {"message": "Not Found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if self.path == "/login/oauth/access_token":  # form-encoded OAuth code exchange
            return self._send(200, {"access_token": "ghu_fakeusertoken000000000000", "token_type": "bearer"})
        data = json.loads(body or b"{}")
        if self.path == "/_push":
            PR["head"]["sha"] = NEW_HEAD
            if UTIL_FILE not in FILES:
                FILES.append(UTIL_FILE)
            return self._send(200, {"head": NEW_HEAD})
        if self.path == "/repos/acme/demo/check-runs":
            CHECKS.append({**data, "id": len(CHECKS) + 1})
            return self._send(201, {"id": len(CHECKS)})
        if self.path == "/app/installations/77/access_tokens":
            return self._send(201, {"token": "ghs_faketoken0000000000000000", "expires_at": "2099-01-01T00:00:00Z"})
        if self.path == "/repos/acme/demo/pulls/1/reviews":
            POSTED.append(data)
            review_id = len(POSTED)
            return self._send(200, {"id": review_id, "html_url": f"https://github.example/acme/demo/pull/1#pullrequestreview-{review_id}"})
        return self._send(404, {"message": "Not Found"})


    def do_PATCH(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(length) or b"{}")
        match = re.fullmatch(r"/repos/acme/demo/check-runs/(\d+)", self.path)
        if match and 0 < int(match.group(1)) <= len(CHECKS):
            CHECKS[int(match.group(1)) - 1].update(data)
            return self._send(200, CHECKS[int(match.group(1)) - 1])
        return self._send(404, {"message": "Not Found"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "9000"))
    print(f"fake-github listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104
