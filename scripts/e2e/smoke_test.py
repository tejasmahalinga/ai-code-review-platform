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
import urllib.parse
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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def new_session():
    """A separate browser: own cookie jar, redirects returned instead of followed."""
    session_jar = http.cookiejar.CookieJar()
    return session_jar, urllib.request.build_opener(urllib.request.HTTPCookieProcessor(session_jar), NoRedirect())


def browser_get(session, path: str) -> tuple[int, str, str]:
    """GET as a browser navigation; returns (status, Location header, body)."""
    _, session_opener = session
    try:
        with session_opener.open(f"{BASE}{path}", timeout=30) as response:
            return response.status, response.headers.get("Location", ""), response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", ""), exc.read().decode()


def team_checks() -> None:
    """ADM-02/03/04: invite a reviewer who joins with GitHub; roles are enforced; it is all audited."""
    status, created = call("POST", "/api/v1/invites", {"email": "dev@example.com", "role": "reviewer"})
    expect(status == 201 and created["url"].startswith("http"), f"invite created ({status})")
    token = created["url"].rsplit("/", 1)[-1]

    dev = new_session()
    status, _, body = browser_get(dev, f"/api/v1/auth/invites/{token}")
    expect(status == 200 and json.loads(body)["role"] == "reviewer", "invite link resolves")
    status, location, _ = browser_get(dev, f"/api/v1/auth/github/start?invite={token}")
    expect(status == 302 and "/login/oauth/authorize?" in location, "sign-in redirects to GitHub")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    expect(query["client_id"] == ["Iv1.e2e"], "GitHub App OAuth client is used")
    status, location, _ = browser_get(
        dev, f"/api/v1/auth/github/callback?code=e2e&state={query['state'][0]}"
    )
    expect(status == 302 and location.endswith("/pull-requests"), f"GitHub callback signs in ({status} {location})")
    status, _, body = browser_get(dev, "/api/v1/auth/me")
    me = json.loads(body)
    expect(me["role"] == "reviewer" and me["github_login"] == "dev-octo", "invitee joined as reviewer via GitHub")
    status, _, _ = browser_get(dev, "/api/v1/llm-credentials")
    expect(status == 403, "reviewer cannot see LLM keys")

    status, users = call("GET", "/api/v1/users")
    expect(status == 200 and {u["email"] for u in users} == {"admin@example.com", "dev@example.com"}, "users listed")
    status, events = call("GET", "/api/v1/audit-events")
    seen = {e["action"] for e in events["results"]}
    expected = {"setup.completed", "credential.created", "user.invited", "invite.accepted", "auth.login"}
    expect(expected <= seen, f"audit log records team and key events ({sorted(seen)})")


def cost_checks() -> None:
    """KEY-05 / ADM-05 / ADM-06: priced usage, budgets, CSV export with a personal API token."""
    status, usage = call("GET", "/api/v1/usage?group_by=model")
    expect(status == 200 and usage["totals"]["unpriced"] == 0, "every LLM call has a price (demo model is free)")
    status, keys = call("GET", "/api/v1/llm-credentials")
    key = keys[0]
    status, key = call("PATCH", f"/api/v1/llm-credentials/{key['id']}", {"monthly_budget_usd": "25.00"})
    expect(status == 200 and key["budget"]["state"] == "ok", f"monthly budget set ({status} {key.get('budget')})")

    status, created = call("POST", "/api/v1/auth/tokens", {"name": "e2e export", "expires_in_days": 7})
    expect(status == 201 and created["token"].startswith("rbt_"), "API token created")
    request = urllib.request.Request(
        f"{BASE}/api/v1/usage?group_by=day,model&export=csv",
        headers={"Authorization": f"Bearer {created['token']}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        body = response.read().decode()
        expect(response.headers["Content-Type"].startswith("text/csv"), "usage CSV export over an API token")
    expect(body.splitlines()[0].startswith("day,model,reviews,requests"), "CSV has the expected columns")


def notification_checks(pull_request_id: int) -> None:
    """Batch D: a signed webhook channel receives a high-risk review notification."""
    hook_url = os.environ.get("NOTIFY_URL", "http://fake-github:9000/_notify")
    status, created = call(
        "POST",
        "/api/v1/notification-channels",
        {
            "name": "e2e hook",
            "kind": "webhook",
            "url": hook_url,
            "secret": "e2e-signing-secret",
            "events": ["review.high_risk", "review.failed"],
            "min_risk": 0,
        },
    )
    expect(status == 201, f"notification channel created ({status} {created})")
    status, result = call("POST", f"/api/v1/notification-channels/{created['id']}/test")
    expect(result == {"ok": True, "error": ""}, f"test notification delivered ({result})")

    status, rerun = call("POST", f"/api/v1/pull-requests/{pull_request_id}/reviews")
    expect(status == 201, f"review re-run for notifications ({status})")

    def high_risk_notification():
        with urllib.request.urlopen(f"{FAKE_GITHUB}/_notifications", timeout=10) as response:  # noqa: S310
            items = json.loads(response.read())
        return next((n for n in items if n["body"]["event"] == "review.high_risk"), None)

    note = wait_for(high_risk_notification, "high-risk notification")
    expect(note["body"]["data"]["review_id"] == rerun["id"], "notification names the review")
    expect(note["headers"].get("x-reviewbot-signature-256", "").startswith("sha256="), "notification is signed")


def gitlab_checks() -> None:
    """INT-03: connect GitLab, enable a project (webhook created), review a merge request end to end."""
    gitlab_url = os.environ.get("GITLAB_URL", "http://fake-github:9000")
    status, data = call("POST", "/api/v1/integrations/gitlab", {"url": gitlab_url, "token": "glpat-e2e-token"})
    expect(status == 201 and data["repositories"] == 1, f"GitLab connected and projects synced ({status} {data})")
    status, info = call("GET", "/api/v1/integrations/gitlab")
    secret = info["webhook_secret"]
    status, repos = call("GET", "/api/v1/repositories")
    project = next(r for r in repos if r["full_name"] == "acme/gl-demo")
    expect(project["provider"] == "gitlab", "GitLab project listed with its provider")
    status, data = call("PATCH", f"/api/v1/repositories/{project['id']}", {"enabled": True})
    expect(status == 200 and data["webhook_managed"] is True, f"project enabled and webhook created ({data})")

    event = {
        "object_kind": "merge_request",
        "user": {"id": 77, "username": "gl-dev"},
        "project": {"id": 2001, "path_with_namespace": "acme/gl-demo"},
        "object_attributes": {
            "iid": 3, "title": "Add GitLab runner", "state": "opened", "action": "open", "draft": False,
            "target_branch": "main", "source_branch": "feature", "last_commit": {"id": "d" * 40},
            "url": "https://gitlab.example/acme/gl-demo/-/merge_requests/3",
        },
    }
    raw = json.dumps(event).encode()
    status, data = call("POST", "/webhooks/gitlab", raw=raw, headers={
        "X-Gitlab-Token": secret, "X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Event-UUID": "e2e-gl-1"})
    expect(status == 202 and data["reason"] == "review_queued", f"merge request webhook accepted ({status} {data})")
    status, bad = call("POST", "/webhooks/gitlab", raw=raw, headers={
        "X-Gitlab-Token": "wrong", "X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Event-UUID": "e2e-gl-2"})
    expect(status == 401, "webhook with a wrong token is rejected")

    status, prs = call("GET", f"/api/v1/pull-requests?repository={project['id']}&state=")
    pr = prs["results"][0]

    def finished():
        review = pr["id"] and call("GET", f"/api/v1/pull-requests/{pr['id']}/reviews")[1]
        return review and review[0]["status"] in ("completed", "failed", "skipped") and review[0]

    run = wait_for(finished, "GitLab review")
    status, run = call("GET", f"/api/v1/reviews/{run['id']}")
    expect(run["status"] == "completed", f"GitLab review completed ({run['status']} {run['error']})")
    with urllib.request.urlopen(f"{FAKE_GITHUB}/_gitlab", timeout=10) as response:  # noqa: S310
        recorded = json.loads(response.read())
    expect(len(recorded["hooks"]) == 1 and recorded["hooks"][0]["token"] == secret, "project hook carries the secret")
    expect(len(recorded["discussions"]) >= 1, "inline findings posted as diff discussions")
    position = recorded["discussions"][0]["position"]
    expect(position["head_sha"] == "d" * 40 and position["new_path"] == "app/runner.py", "discussion is anchored")
    expect(len(recorded["notes"]) == 1 and "Reviewbot" in recorded["notes"][0]["body"], "summary posted as a note")
    states = [s["state"] for s in recorded["statuses"]]
    expect(states[:1] == ["running"] and states[-1] in ("success", "failed"), f"commit status reported ({states})")


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
        {
            "app_id": "1",
            "app_slug": "reviewbot-e2e",
            "private_key": private_key(),
            "webhook_secret": WEBHOOK_SECRET,
            "client_id": "Iv1.e2e",
            "client_secret": "e2e-client-secret",
        },
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
    expect(run["config_source"] == ".reviewbot.yml" and not run["config_error"], ".reviewbot.yml from base applied")
    expect(isinstance(run["risk_score"], int) and run["risk_score"] >= 30, f"risk score recorded ({run['risk_score']})")
    todo = next(f for f in run["findings"] if f["title"] == "Unresolved TODO")
    status, data = call("PATCH", f"/api/v1/findings/{todo['id']}", {"state": "dismissed", "dismiss_reason": "wont_fix"})
    expect(status == 200 and data["state"] == "dismissed", "finding dismissed")
    status, data = call("PUT", f"/api/v1/findings/{todo['id']}/feedback", {"vote": "down"})
    expect(status == 200 and data["votes"]["down"] == 1, "feedback vote recorded")

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
    status, rerun_detail = call("GET", f"/api/v1/reviews/{rerun['id']}")
    statuses = {f["title"]: f["post_status"] for f in rerun_detail["findings"]}
    expect(statuses.get("Unresolved TODO") == "dismissed_earlier", "dismissed finding stays dismissed on re-run")
    status, diff = call("GET", f"/api/v1/reviews/{rerun['id']}/compare?with={run['id']}")
    expect(status == 200 and "unchanged" in diff, "runs can be compared")

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

    # A maintainer asks for a fresh review from a PR comment.
    comment = {
        "action": "created",
        "installation": {"id": 77},
        "repository": {"id": 4242, "full_name": "acme/demo"},
        "issue": {"number": 1, "title": "Add expression runner", "pull_request": {"url": "x"}, "user": {"login": "octocat"}},
        "comment": {"body": "/reviewbot review", "author_association": "OWNER", "user": {"login": "maintainer", "type": "User"}},
    }
    comment_raw = json.dumps(comment).encode()
    comment_sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), comment_raw, hashlib.sha256).hexdigest()
    status, data = call("POST", "/webhooks/github", raw=comment_raw, headers={"X-Hub-Signature-256": comment_sig,
                                                                              "X-GitHub-Event": "issue_comment",
                                                                              "X-GitHub-Delivery": "e2e-comment"})
    expect(status == 202 and data["reason"] == "review_queued", f"/reviewbot review accepted ({status} {data})")
    command_run_id = run_id_from_history(run["pull_request"]["id"], "command")
    wait_for(lambda: call("GET", f"/api/v1/reviews/{command_run_id}")[1]["status"] == "completed", "command review")

    status, usage = call("GET", "/api/v1/usage?group_by=credential")
    expect(usage["totals"]["requests"] >= 2, "usage endpoint aggregates calls")

    team_checks()
    cost_checks()
    notification_checks(run["pull_request"]["id"])
    gitlab_checks()
    print("\nEnd-to-end smoke test passed.")


if __name__ == "__main__":
    main()
