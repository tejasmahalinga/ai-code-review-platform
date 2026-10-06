#!/usr/bin/env python3
"""End-to-end smoke test of the MVP flow (stdlib only).

Requires a running stack whose API talks to scripts/e2e/fake_github.py (GITHUB_API_URL) and has
REVIEWBOT_ENABLE_FAKE_PROVIDER=true. Exercises, through the dashboard origin:
setup admin → add LLM key → connect GitHub App → sync → enable repo → signed PR webhook →
review posted on GitHub with inline comments and a check run → findings in the dashboard → re-run without
duplicates → new push reviewed incrementally.

Usage: BASE_URL=http://localhost:3000 FAKE_GITHUB_URL=http://localhost:9000 python3 smoke_test.py
"""

from __future__ import annotations

import hashlib
import hmac
import http.cookiejar
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://localhost:3000").rstrip("/")
FAKE_GITHUB = os.environ.get("FAKE_GITHUB_URL", "http://localhost:9000").rstrip("/")
WEBHOOK_SECRET = "e2e-webhook-secret"

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def csrf_token() -> str:
    return next((c.value for c in jar if c.name == "csrftoken"), "")


def call(method: str, path: str, body: object = None, *, headers: dict | None = None, raw: bytes | None = None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    request = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Origin", BASE)
    request.add_header("Referer", BASE + "/")
    if method != "GET":
        request.add_header("X-CSRFToken", csrf_token())
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with opener.open(request, timeout=30) as response:
            text = response.read().decode()
            return response.status, (json.loads(text) if text else None)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode()
        try:
            return exc.code, json.loads(text)
        except ValueError:
            return exc.code, text


def expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr)
        sys.exit(1)
    print(f"ok   {message}")


def wait_for(fn, what: str, timeout: float = 120.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(1)
    expect(False, f"timed out waiting for {what}")


def run_id_from_history(pull_request_id: int, trigger: str) -> int | None:
    _, history = call("GET", f"/api/v1/pull-requests/{pull_request_id}/reviews")
    return next((h["id"] for h in history if h["trigger"] == trigger), None)


def private_key() -> str:
    return subprocess.run(
        ["openssl", "genrsa", "2048"], check=True, capture_output=True, text=True
    ).stdout


def main() -> None:
    wait_for(lambda: call("GET", "/readyz")[0] == 200, "API readiness")

    call("GET", "/api/v1/auth/csrf")
    status, data = call("GET", "/api/v1/setup/status")
    expect(status == 200 and data["needs_setup"] is True, "fresh instance needs setup")
    status, data = call("POST", "/api/v1/setup", {"email": "admin@example.com", "password": "e2e-strong-passphrase"})
    expect(status == 201, f"admin created ({status} {data})")

    status, cred = call("POST", "/api/v1/llm-credentials", {"name": "Demo", "provider": "fake", "default_model": "demo"})
    expect(status == 201 and cred["status"] == "valid", "LLM credential added and validated")

    status, data = call(
        "POST",
        "/api/v1/integrations/github/manual",
        {"app_id": "1", "app_slug": "reviewbot-e2e", "private_key": private_key(), "webhook_secret": WEBHOOK_SECRET},
    )
    expect(status == 201, f"GitHub App configured ({status} {data})")
    status, data = call("POST", "/api/v1/integrations/github/sync")
    expect(status == 200 and data == {"installations": 1, "repositories": 1}, "installation and repo synced")

    status, repos = call("GET", "/api/v1/repositories")
    repo = repos[0]
    expect(repo["full_name"] == "acme/demo" and repo["enabled"] is False, "repo listed, disabled by default")
    status, data = call("PATCH", f"/api/v1/repositories/{repo['id']}", {"enabled": True})
    expect(status == 200 and data["enabled"], "repo enabled")

    payload = {
        "action": "opened",
        "installation": {"id": 77},
        "repository": {"id": 4242, "full_name": "acme/demo"},
        "pull_request": {
            "number": 1, "title": "Add expression runner", "user": {"login": "octocat"}, "state": "open",
            "draft": False, "html_url": "https://github.example/acme/demo/pull/1",
            "base": {"ref": "main", "sha": "b" * 40}, "head": {"ref": "feature", "sha": "a" * 40},
        },
    }
    raw = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    status, _ = call("POST", "/webhooks/github", raw=raw, headers={"X-Hub-Signature-256": "sha256=bad",
                                                                   "X-GitHub-Event": "pull_request",
                                                                   "X-GitHub-Delivery": "e2e-0"})
    expect(status == 401, "webhook with bad signature rejected")
    status, data = call("POST", "/webhooks/github", raw=raw, headers={"X-Hub-Signature-256": signature,
                                                                      "X-GitHub-Event": "pull_request",
                                                                      "X-GitHub-Delivery": "e2e-1"})
    expect(status == 202 and data["reason"] == "review_queued", f"webhook accepted ({status} {data})")

    def completed_review():
        _, prs = call("GET", "/api/v1/pull-requests")
        latest = prs["results"][0]["latest_review"] if prs["results"] else None
        return latest if latest and latest["status"] in ("completed", "failed", "skipped") else None

    latest = wait_for(completed_review, "review to finish")
    expect(latest["status"] == "completed", f"review completed ({latest})")
    status, run = call("GET", f"/api/v1/reviews/{latest['id']}")
    titles = {f["title"]: f["post_status"] for f in run["findings"]}
    expect(titles.get("Use of eval()") == "posted", "critical eval() finding posted inline")
    expect(titles.get("Hard-coded credential") == "posted", "hard-coded credential finding posted inline")
    expect(any(f["path"] == "package-lock.json" for f in run["files_ignored"]), "lockfile ignored")
    expect(run["input_tokens"] > 0, "token usage recorded")

    with urllib.request.urlopen(f"{FAKE_GITHUB}/_posted", timeout=10) as response:  # noqa: S310
        posted = json.loads(response.read())
    expect(len(posted) == 1 and posted[0]["event"] == "COMMENT", "one COMMENT review posted to GitHub")
    expect(len(posted[0]["comments"]) >= 2, "review contains inline comments")
    expect("Reviewbot review" in posted[0]["body"], "review contains summary body")

    status, rerun = call("POST", f"/api/v1/pull-requests/{run['pull_request']['id']}/reviews")
    expect(status == 201, "manual re-run queued")
    wait_for(lambda: call("GET", f"/api/v1/reviews/{rerun['id']}")[1]["status"] == "completed", "re-run to finish")
    with urllib.request.urlopen(f"{FAKE_GITHUB}/_posted", timeout=10) as response:  # noqa: S310
        posted = json.loads(response.read())
    expect(len(posted) == 2 and posted[1]["comments"] == [], "re-run did not duplicate inline comments")

    with urllib.request.urlopen(f"{FAKE_GITHUB}/_checks", timeout=10) as response:  # noqa: S310
        checks = json.loads(response.read())
    expect(len(checks) >= 1 and checks[0].get("status") == "completed", "check run created and completed")
    expect(checks[0].get("conclusion") == "neutral", "check run is neutral with findings and no gate")

    # A new commit is pushed to the PR: only the change since the last reviewed commit is reviewed.
    urllib.request.urlopen(urllib.request.Request(f"{FAKE_GITHUB}/_push", data=b"{}", method="POST"), timeout=10)  # noqa: S310
    push_payload = json.loads(raw)
    push_payload["action"] = "synchronize"
    push_payload["pull_request"]["head"]["sha"] = "c" * 40
    push_raw = json.dumps(push_payload).encode()
    push_sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), push_raw, hashlib.sha256).hexdigest()
    status, data = call("POST", "/webhooks/github", raw=push_raw, headers={"X-Hub-Signature-256": push_sig,
                                                                           "X-GitHub-Event": "pull_request",
                                                                           "X-GitHub-Delivery": "e2e-push"})
    expect(status == 202 and data["reason"].startswith("push_review_queued"), f"push accepted ({status} {data})")
    push_run_id = data and run_id_from_history(run["pull_request"]["id"], "push")
    expect(bool(push_run_id), "push review run created")
    push_run = wait_for(
        lambda: (r := call("GET", f"/api/v1/reviews/{push_run_id}")[1])["status"] in ("completed", "failed", "skipped") and r,
        "push review to finish",
    )
    expect(push_run["status"] == "completed", f"push review completed ({push_run['status']} {push_run['error']})")
    expect(push_run["incremental"] is True and push_run["compare_base_sha"] == "a" * 40, "push review is incremental")
    expect([f["path"] for f in push_run["files_reviewed"]] == ["app/util.py"], "only the new change was reviewed")

    status, usage = call("GET", "/api/v1/usage?group_by=credential")
    expect(usage["totals"]["requests"] >= 2, "usage endpoint aggregates calls")
    print("\nEnd-to-end smoke test passed.")


if __name__ == "__main__":
    main()
