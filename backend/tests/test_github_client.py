from __future__ import annotations

import json

import httpx2
import jwt
import pytest

from apps.git_providers.base import GitProviderError, InlineComment
from apps.git_providers.github import app as github_app
from apps.git_providers.github.client import (
    GitHubAppClient,
    InlineCommentsRejected,
    clear_token_cache,
    exchange_manifest_code,
)
from tests.factories import cached_pem
from tests.http import MockRouter, body

API = "https://api.github.com"


@pytest.fixture(autouse=True)
def _clean_tokens():
    clear_token_cache()
    yield
    clear_token_cache()


@pytest.fixture
def router():
    r = MockRouter()
    r.add(
        "POST",
        r"/app/installations/77/access_tokens$",
        {"token": "ghs_installtoken123456789012345", "expires_at": "x"},
    )
    return r


def client(router):
    return GitHubAppClient(
        api_url=API, app_id="12345", private_key=cached_pem(), http_client=router.client()
    ).for_installation(77)


def test_installation_token_uses_app_jwt_and_is_cached(router):
    router.add(
        "GET",
        r"/repos/acme/app/pulls/1$",
        {"number": 1, "title": "t", "state": "open", "head": {"sha": "h"}, "base": {"sha": "b"}},
    )
    c = client(router)
    c.get_pull_request("acme/app", 1)
    c.get_pull_request("acme/app", 1)
    token_calls = router.routes[0].calls
    assert len(token_calls) == 1
    app_token = token_calls[0].headers["authorization"].removeprefix("Bearer ")
    claims = jwt.decode(app_token, options={"verify_signature": False})
    assert claims["iss"] == "12345"
    assert claims["exp"] - claims["iat"] <= 600
    assert router.routes[1].calls[0].headers["authorization"] == "token ghs_installtoken123456789012345"


def test_list_files_paginates(router):
    page1 = [
        {
            "filename": f"f{i}.py",
            "status": "modified",
            "additions": 1,
            "deletions": 0,
            "patch": "@@ -1 +1 @@\n+x",
        }
        for i in range(100)
    ]
    page2 = [{"filename": "last.png", "status": "added", "additions": 0, "deletions": 0}]

    def handler(request):
        page = request.url.params.get("page")
        return httpx2.Response(200, json=page1 if page == "1" else page2)

    router.add("GET", r"/pulls/1/files", handler=handler)
    files = client(router).list_files("acme/app", 1, max_files=500)
    assert len(files) == 101
    assert files[-1].patch is None


def test_post_review_payload_and_comment_urls(router):
    route = router.add("POST", r"/pulls/1/reviews$", {"id": 9, "html_url": "https://github.com/r#9"})
    router.add(
        "GET",
        r"/reviews/9/comments",
        [{"id": 55, "path": "a.py", "line": 4, "html_url": "https://github.com/c55"}],
    )
    review = client(router).post_review(
        "acme/app",
        1,
        commit_sha="abc",
        body="summary",
        comments=[InlineComment("a.py", 4, "body", start_line=2)],
    )
    sent = body(route.calls[0])
    assert sent["event"] == "COMMENT"
    assert sent["commit_id"] == "abc"
    assert sent["comments"] == [
        {"path": "a.py", "line": 4, "side": "RIGHT", "body": "body", "start_line": 2, "start_side": "RIGHT"}
    ]
    assert review.comments[("a.py", 4)] == ("55", "https://github.com/c55")


def test_invalid_inline_position_raises_specific_error(router):
    router.add(
        "POST", r"/pulls/1/reviews$", {"message": "Unprocessable Entity", "errors": ["line"]}, status=422
    )
    with pytest.raises(InlineCommentsRejected):
        client(router).post_review(
            "acme/app", 1, commit_sha="abc", body="s", comments=[InlineComment("a.py", 999, "x")]
        )


def test_rate_limit_with_long_reset_raises_retryable(router):
    router.add(
        "GET",
        r"/pulls/1$",
        {"message": "API rate limit exceeded"},
        status=403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"},
    )
    with pytest.raises(GitProviderError) as exc:
        client(router).get_pull_request("acme/app", 1)
    assert exc.value.retryable


def test_secondary_rate_limit_short_retry_after_is_retried(router, monkeypatch):
    monkeypatch.setattr("apps.git_providers.github.client.time.sleep", lambda s: None)
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx2.Response(403, json={"message": "secondary"}, headers={"retry-after": "1"})
        return httpx2.Response(
            200, json={"number": 1, "title": "t", "state": "open", "head": {"sha": "h"}, "base": {}}
        )

    router.add("GET", r"/pulls/1$", handler=handler)
    assert client(router).get_pull_request("acme/app", 1).head_sha == "h"
    assert attempts["n"] == 2


def test_not_found_is_not_retryable(router):
    router.add("GET", r"/pulls/1$", {"message": "Not Found"}, status=404)
    with pytest.raises(GitProviderError) as exc:
        client(router).get_pull_request("acme/app", 1)
    assert exc.value.status == 404 and not exc.value.retryable


def test_manifest_exchange():
    router = MockRouter()
    route = router.add("POST", r"/app-manifests/CODE/conversions$", {"id": 1, "slug": "x", "pem": "k"})
    assert exchange_manifest_code(API, "CODE", router.client())["slug"] == "x"
    assert "authorization" not in route.calls[0].headers


def test_manifest_requests_least_privilege(settings):
    manifest = json.loads(github_app.manifest_json("Reviewbot"))
    assert manifest["default_permissions"] == {
        "pull_requests": "write",
        "contents": "read",
        "metadata": "read",
        "checks": "write",
    }
    assert manifest["default_events"] == ["pull_request"]
    assert manifest["hook_attributes"]["url"] == f"{settings.PUBLIC_URL}/webhooks/github"
    assert manifest["public"] is False


def test_signature_verification():
    secret, payload = "s3cret", b'{"a":1}'
    import hashlib
    import hmac

    good = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    assert github_app.verify_signature(secret, payload, good)
    assert not github_app.verify_signature(secret, payload + b" ", good)
    assert not github_app.verify_signature(secret, payload, None)
    assert not github_app.verify_signature("", payload, good)


def test_compare_parses_status_and_files(router):
    route = router.add(
        "GET",
        r"/repos/acme/app/compare/aaa\.\.\.ccc$",
        {
            "status": "ahead",
            "files": [
                {
                    "filename": "a.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 0,
                    "patch": "@@ -1 +1 @@\n-x\n+y",
                }
            ],
        },
    )
    result = client(router).compare("acme/app", "aaa", "ccc")
    assert result.status == "ahead"
    assert [f.path for f in result.files] == ["a.py"]
    assert route.calls[0].headers["authorization"].startswith("token ")


def test_check_run_lifecycle(router):
    create = router.add("POST", r"/repos/acme/app/check-runs$", {"id": 321})
    update = router.add("PATCH", r"/repos/acme/app/check-runs/321$", {"id": 321})
    c = client(router)
    check_id = c.create_check_run("acme/app", "abc", name="Reviewbot", details_url="https://rb/reviews/1")
    assert check_id == "321"
    sent = body(create.calls[0])
    assert (sent["name"], sent["head_sha"], sent["status"], sent["details_url"]) == (
        "Reviewbot",
        "abc",
        "in_progress",
        "https://rb/reviews/1",
    )
    c.complete_check_run("acme/app", "321", conclusion="failure", title="t" * 300, summary="s" * 70_000)
    patch = body(update.calls[0])
    assert (patch["status"], patch["conclusion"]) == ("completed", "failure")
    assert len(patch["output"]["title"]) == 255
    assert len(patch["output"]["summary"]) == 60_000
