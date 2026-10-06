"""INT-03 GitLab: REST client, connection and sync, project webhooks, webhook events, pipeline integration."""

from __future__ import annotations

import json
import uuid
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest

from apps.audit.models import AuditEvent
from apps.git_providers import registry
from apps.git_providers.base import GitProviderError, InlineComment
from apps.git_providers.fake import FakeGitProvider
from apps.git_providers.gitlab.client import (
    GitLabClient,
    changed_file,
    gitlab_suggestion,
    merge_request_from_payload,
)
from apps.repositories.models import GitProviderConnection, Installation, Repository
from apps.reviews import pipeline
from apps.reviews.models import Finding, PullRequest, ReviewRun
from apps.reviews.services import create_run
from tests.factories import EnqueueRecorder, ScriptedLLM, added_file, finding, make_pull_request, pr_info
from tests.http import MockRouter, body

API = "https://gitlab.example/api/v4"
PROJECT = "acme%2Fapp"
SECRET = "gitlab-webhook-secret"

PATCH = "@@ -1,2 +1,3 @@\n line one\n-old\n+new\n+added\n"
MR = {
    "iid": 7,
    "title": "Add things",
    "author": {"username": "dev"},
    "state": "opened",
    "draft": False,
    "target_branch": "main",
    "source_branch": "feature",
    "sha": "h" * 40,
    "diff_refs": {"base_sha": "b" * 40, "start_sha": "s" * 40, "head_sha": "h" * 40},
    "web_url": "https://gitlab.example/acme/app/-/merge_requests/7",
}


def client(router: MockRouter) -> GitLabClient:
    return GitLabClient(API, "glpat-test-token-1234567890", router.client())


# --- pure helpers ------------------------------------------------------------------------------


def test_changed_file_mapping():
    added = changed_file({"new_path": "a.py", "old_path": "a.py", "new_file": True, "diff": PATCH})
    assert (added.status, added.additions, added.deletions, added.patch) == ("added", 2, 1, PATCH)
    renamed = changed_file({"new_path": "b.py", "old_path": "a.py", "renamed_file": True, "diff": ""})
    assert (renamed.status, renamed.previous_path, renamed.patch) == ("renamed", "a.py", None)
    binary = changed_file({"new_path": "x.png", "old_path": "x.png", "diff": "Binary files differ\n"})
    assert binary.patch is None and binary.additions == 0


def test_merge_request_mapping():
    info = merge_request_from_payload(MR)
    assert (info.number, info.author_login, info.state, info.base_sha, info.head_sha) == (
        7,
        "dev",
        "open",
        "b" * 40,
        "h" * 40,
    )
    assert merge_request_from_payload({**MR, "state": "merged"}).state == "merged"
    assert merge_request_from_payload({**MR, "state": "locked"}).state == "closed"
    assert merge_request_from_payload({**MR, "draft": None, "work_in_progress": True}).is_draft


def test_suggestion_syntax():
    body = "Fix it\n\n```suggestion\nnew()\n```"
    assert "```suggestion:-0+0\n" in gitlab_suggestion(body, InlineComment(path="a", line=5, body=body))
    multi = InlineComment(path="a", line=5, start_line=3, body=body)
    assert "```suggestion:-2+0\n" in gitlab_suggestion(body, multi)


# --- client ------------------------------------------------------------------------------------


def test_get_and_list_files_with_pagination():
    router = MockRouter()
    router.add("GET", rf"/projects/{PROJECT}/merge_requests/7$", MR)
    pages = {
        "1": ([{"new_path": f"f{i}.py", "old_path": f"f{i}.py", "diff": PATCH} for i in range(100)], "2"),
        "2": ([{"new_path": "last.py", "old_path": "last.py", "diff": PATCH}], ""),
    }

    def diffs(request: httpx2.Request) -> httpx2.Response:
        items, next_page = pages[parse_qs(urlparse(str(request.url)).query)["page"][0]]
        return httpx2.Response(200, json=items, headers={"x-next-page": next_page})

    router.add("GET", r"/merge_requests/7/diffs", handler=diffs)
    gl = client(router)
    assert gl.get_pull_request("acme/app", 7).title == "Add things"
    files = gl.list_files("acme/app", 7, max_files=500)
    assert len(files) == 101 and files[-1].path == "last.py"
    assert router.requests[0].headers["PRIVATE-TOKEN"] == "glpat-test-token-1234567890"
    assert len(gl.list_files("acme/app", 7, max_files=50)) == 50


def test_list_files_falls_back_to_changes_on_old_gitlab():
    router = MockRouter()
    router.add("GET", r"/merge_requests/7/diffs", {"message": "404 Not Found"}, status=404)
    router.add(
        "GET",
        r"/merge_requests/7/changes$",
        {"changes": [{"new_path": "a.py", "old_path": "a.py", "diff": PATCH}]},
    )
    assert [f.path for f in client(router).list_files("acme/app", 7, 10)] == ["a.py"]


def test_diff_not_ready_is_retryable():
    router = MockRouter()
    router.add("GET", r"/merge_requests/7$", {**MR, "diff_refs": None})
    with pytest.raises(GitProviderError) as excinfo:
        client(router).get_pull_request("acme/app", 7)
    assert excinfo.value.retryable


def test_post_review_places_discussions_and_reports_rejected():
    router = MockRouter()
    router.add("GET", r"/merge_requests/7$", MR)
    router.add(
        "GET",
        r"/merge_requests/7/versions$",
        [{"head_commit_sha": "h" * 40, "base_commit_sha": "B" * 40, "start_commit_sha": "S" * 40}],
    )

    def discussion(request: httpx2.Request) -> httpx2.Response:
        payload = body(request)
        if payload["position"]["new_line"] == 99:
            return httpx2.Response(
                400, json={"message": '400 Bad request - Note {:line_code=>["can\'t be blank"]}'}
            )
        return httpx2.Response(201, json={"id": "d1", "notes": [{"id": 555}]})

    discussions = router.add("POST", r"/merge_requests/7/discussions$", handler=discussion)
    notes = router.add("POST", r"/merge_requests/7/notes$", {"id": 900})
    review = client(router).post_review(
        "acme/app",
        7,
        commit_sha="h" * 40,
        body="Summary",
        comments=[
            InlineComment(path="a.py", line=3, body="Bug\n\n```suggestion\nfix()\n```"),
            InlineComment(path="a.py", line=99, body="Elsewhere"),
        ],
    )
    first = body(discussions.calls[0])
    assert first["position"] == {
        "position_type": "text",
        "base_sha": "B" * 40,
        "start_sha": "S" * 40,
        "head_sha": "h" * 40,
        "old_path": "a.py",
        "new_path": "a.py",
        "new_line": 3,
    }
    assert "```suggestion:-0+0" in first["body"]
    assert review.comments == {("a.py", 3): ("555", f"{MR['web_url']}#note_555")}
    assert review.rejected == {("a.py", 99)}
    summary = body(notes.calls[0])["body"]
    assert summary.startswith("Summary") and "`a.py` line 99" in summary and "Elsewhere" in summary
    assert (review.id, review.html_url) == ("900", f"{MR['web_url']}#note_900")


def test_server_errors_on_inline_comments_propagate():
    router = MockRouter()
    router.add("GET", r"/merge_requests/7$", MR)
    router.add("GET", r"/merge_requests/7/versions$", [])
    router.add("POST", r"/discussions$", {"message": "forbidden"}, status=403)
    with pytest.raises(GitProviderError) as excinfo:
        client(router).post_review(
            "acme/app", 7, commit_sha="h" * 40, body="x", comments=[InlineComment("a.py", 1, "b")]
        )
    assert excinfo.value.status == 403


def test_get_file():
    router = MockRouter()
    router.add("GET", rf"/projects/{PROJECT}/repository/files/\.reviewbot\.yml/raw", "profile: strict\n")
    router.add("GET", r"/repository/files/missing\.yml/raw", {"message": "404 File Not Found"}, status=404)
    gl = client(router)
    assert gl.get_file("acme/app", ".reviewbot.yml", "b" * 40) == "profile: strict\n"
    assert parse_qs(urlparse(str(router.requests[0].url)).query)["ref"] == ["b" * 40]
    assert gl.get_file("acme/app", "missing.yml", "main") is None


def test_compare_ahead_and_force_push():
    router = MockRouter()
    merge_base = {"id": "a" * 40}
    router.add("GET", r"/repository/merge_base", handler=lambda _r: httpx2.Response(200, json=merge_base))
    router.add(
        "GET", r"/repository/compare", {"diffs": [{"new_path": "n.py", "old_path": "n.py", "diff": PATCH}]}
    )
    gl = client(router)
    ahead = gl.compare("acme/app", "a" * 40, "c" * 40)
    assert ahead.status == "ahead" and [f.path for f in ahead.files] == ["n.py"]
    merge_base["id"] = "z" * 40
    assert gl.compare("acme/app", "a" * 40, "c" * 40).status == "diverged"
    assert gl.compare("acme/app", "a" * 40, "a" * 40).status == "identical"


@pytest.mark.parametrize(
    ("conclusion", "state"),
    [
        ("success", "success"),
        ("neutral", "success"),
        ("skipped", "success"),
        ("failure", "failed"),
        ("cancelled", "canceled"),
    ],
)
def test_commit_status_lifecycle(conclusion, state):
    router = MockRouter()
    statuses = router.add("POST", rf"/projects/{PROJECT}/statuses/h+$", {"id": 1})
    gl = client(router)
    check_id = gl.create_check_run(
        "acme/app", "h" * 40, name="Reviewbot", details_url="https://rb.example/reviews/1"
    )
    gl.complete_check_run(
        "acme/app",
        check_id,
        conclusion=conclusion,
        title="2 finding(s)",
        summary="x\n\n[Open](https://rb.example/reviews/1)",
    )
    started, finished = (body(c) for c in statuses.calls)
    assert started == {
        "state": "running",
        "name": "Reviewbot",
        "description": "Review in progress",
        "target_url": "https://rb.example/reviews/1",
    }
    assert finished["state"] == state and finished["target_url"] == "https://rb.example/reviews/1"


def test_rate_limit_is_retryable(monkeypatch):
    monkeypatch.setattr("apps.git_providers.gitlab.client.time.sleep", lambda _s: None)
    router = MockRouter()
    router.add(
        "GET", r"/merge_requests/7$", {"message": "slow down"}, status=429, headers={"Retry-After": "120"}
    )
    with pytest.raises(GitProviderError) as excinfo:
        client(router).get_pull_request("acme/app", 7)
    assert excinfo.value.retry_after == 120 and excinfo.value.retryable


def test_member_access_level():
    router = MockRouter()
    router.add("GET", r"/members/all/5$", {"access_level": 40})
    router.add("GET", r"/members/all/6$", {"message": "404"}, status=404)
    gl = client(router)
    assert gl.member_access_level("acme/app", 5) == 40
    assert gl.member_access_level("acme/app", 6) == 0


# --- connection, sync, webhooks setup ------------------------------------------------------------


@pytest.fixture
def gitlab_api(monkeypatch):
    """Routes every GitLab client to one MockRouter."""
    router = MockRouter()
    router.add(
        "GET",
        r"/api/v4/user$",
        {"id": 42, "username": "reviewbot", "web_url": "https://gitlab.example/reviewbot"},
    )
    router.add("GET", r"/personal_access_tokens/self$", {"scopes": ["api"]})
    router.add(
        "GET",
        r"/api/v4/projects\?",
        [
            {
                "id": 1001,
                "path_with_namespace": "acme/app",
                "path": "app",
                "visibility": "private",
                "default_branch": "main",
                "web_url": "https://gitlab.example/acme/app",
            },
            {
                "id": 1002,
                "path_with_namespace": "acme/site",
                "path": "site",
                "visibility": "public",
                "default_branch": "main",
                "web_url": "https://gitlab.example/acme/site",
            },
        ],
    )
    import apps.git_providers.gitlab.client as module

    original = module.GitLabClient.__init__

    def init(self, api_url, token, http_client=None):
        original(self, api_url, token, router.client())

    monkeypatch.setattr(module.GitLabClient, "__init__", init)
    return router


@pytest.mark.django_db
def test_connect_syncs_projects(api, gitlab_api):
    response = api.post(
        "/api/v1/integrations/gitlab", {"url": "https://gitlab.example/", "token": "glpat-abc"}, format="json"
    )
    assert response.status_code == 201, response.content
    assert response.json() == {"installations": 1, "repositories": 2}
    connection = GitProviderConnection.objects.get(provider="gitlab")
    assert connection.api_url == "https://gitlab.example/api/v4" and connection.access_token == "glpat-abc"
    assert "glpat-abc" not in connection.encrypted_access_token
    assert Installation.objects.get(connection=connection).account_login == "reviewbot"
    repo = Repository.objects.get(full_name="acme/site")
    assert repo.private is False and repo.html_url == "https://gitlab.example/acme/site"
    info = api.get("/api/v1/integrations/gitlab").json()
    assert info["connected"] and info["username"] == "reviewbot" and info["repositories"] == 2
    assert (
        info["webhook_url"].endswith("/webhooks/gitlab")
        and info["webhook_secret"] == connection.webhook_secret
    )
    assert AuditEvent.objects.filter(action="integration.gitlab_connected").exists()
    assert "glpat" not in json.dumps(list(AuditEvent.objects.values_list("metadata", flat=True)))
    assert api.post("/api/v1/integrations/gitlab", {"token": "x"}, format="json").status_code == 409


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("http://gitlab:8080", True),
        ("https://gitlab.internal.example/", True),
        ("ftp://gitlab.example", False),
        ("https://user:pw@gitlab.example", False),
        ("gitlab.example", False),
    ],
)
def test_connect_url_validation(url, ok):
    from apps.repositories.views import GitLabConnectSerializer

    serializer = GitLabConnectSerializer(data={"url": url, "token": "t"})
    assert serializer.is_valid() is ok
    if ok:
        assert not serializer.validated_data["url"].endswith("/")


@pytest.mark.django_db
def test_connect_rejects_tokens_without_api_scope(api, gitlab_api):
    gitlab_api.routes.insert(
        0, gitlab_api.add("GET", r"/personal_access_tokens/self$", {"scopes": ["read_api"]})
    )
    response = api.post("/api/v1/integrations/gitlab", {"token": "glpat-ro"}, format="json")
    assert response.status_code == 400 and "api" in json.dumps(response.json())


@pytest.fixture
def gitlab_repo(api, gitlab_api, fake_credential):
    api.post(
        "/api/v1/integrations/gitlab", {"url": "https://gitlab.example", "token": "glpat-abc"}, format="json"
    )
    repo = Repository.objects.get(full_name="acme/app")
    repo.settings.credential = fake_credential
    repo.settings.save()
    return repo


@pytest.mark.django_db
def test_enabling_creates_project_webhook(api, gitlab_api, gitlab_repo):
    hooks = gitlab_api.add("POST", r"/projects/1001/hooks$|/projects/acme%2Fapp/hooks$", {"id": 31})
    response = api.patch(f"/api/v1/repositories/{gitlab_repo.pk}", {"enabled": True}, format="json")
    assert response.status_code == 200 and "webhook_warning" not in response.json()
    assert response.json()["provider"] == "gitlab" and response.json()["webhook_managed"] is True
    payload = body(hooks.calls[0])
    assert (
        payload["url"].endswith("/webhooks/gitlab")
        and payload["merge_requests_events"]
        and payload["note_events"]
    )
    assert payload["token"] == GitProviderConnection.objects.get(provider="gitlab").webhook_secret
    # Enabling again keeps the existing hook.
    gitlab_api.add("GET", r"/hooks/31$", {"id": 31})
    api.patch(f"/api/v1/repositories/{gitlab_repo.pk}", {"enabled": False}, format="json")
    api.patch(f"/api/v1/repositories/{gitlab_repo.pk}", {"enabled": True}, format="json")
    assert len(hooks.calls) == 1


@pytest.mark.django_db
def test_enabling_without_maintainer_role_warns(api, gitlab_api, gitlab_repo):
    gitlab_api.add("POST", r"/hooks$", {"message": "403 Forbidden"}, status=403)
    response = api.patch(f"/api/v1/repositories/{gitlab_repo.pk}", {"enabled": True}, format="json")
    assert response.status_code == 200
    assert response.json()["enabled"] is True and "Maintainer" in response.json()["webhook_warning"]
    assert response.json()["webhook_managed"] is False


@pytest.mark.django_db
def test_registry_returns_gitlab_client(gitlab_repo):
    registry.set_factory(None)
    assert isinstance(registry.provider_for(gitlab_repo), GitLabClient)


# --- webhooks ------------------------------------------------------------------------------------


@pytest.fixture
def enabled_repo(gitlab_repo):
    Repository.objects.filter(pk=gitlab_repo.pk).update(enabled=True, webhook_id="31")
    gitlab_repo.refresh_from_db()
    return gitlab_repo


@pytest.fixture
def enqueued(monkeypatch):
    calls = EnqueueRecorder()
    monkeypatch.setattr("apps.reviews.services.enqueue", calls)
    return calls


def hook(client, event: str, payload: dict, *, token: str | None = None, uuid_: str | None = None):
    secret = (
        token if token is not None else GitProviderConnection.objects.get(provider="gitlab").webhook_secret
    )
    return client.post(
        "/webhooks/gitlab",
        data=json.dumps(payload).encode(),
        content_type="application/json",
        HTTP_X_GITLAB_TOKEN=secret,
        HTTP_X_GITLAB_EVENT=event,
        HTTP_X_GITLAB_EVENT_UUID=uuid_ or str(uuid.uuid4()),
    )


def mr_event(action: str, **attrs) -> dict:
    return {
        "object_kind": "merge_request",
        "user": {"id": 7, "username": "dev"},
        "project": {"id": 1001, "path_with_namespace": "acme/app"},
        "object_attributes": {
            "iid": 7,
            "title": "Add things",
            "state": "opened",
            "action": action,
            "draft": False,
            "target_branch": "main",
            "source_branch": "feature",
            "last_commit": {"id": "h" * 40},
            "url": MR["web_url"],
            **attrs,
        },
        "changes": attrs.pop("changes", {}) if "changes" in attrs else {},
    }


@pytest.mark.django_db
def test_webhook_token_is_required(anon, enabled_repo, enqueued):
    assert hook(anon, "Merge Request Hook", mr_event("open"), token="wrong").status_code == 401
    assert hook(anon, "Merge Request Hook", mr_event("open"), token="").status_code == 401
    assert ReviewRun.objects.count() == 0


@pytest.mark.django_db
def test_opened_merge_request_queues_review(anon, enabled_repo, enqueued):
    delivery = str(uuid.uuid4())
    response = hook(anon, "Merge Request Hook", mr_event("open"), uuid_=delivery)
    assert response.status_code == 202 and response.json()["reason"] == "review_queued"
    pr = PullRequest.objects.get(repository=enabled_repo, number=7)
    assert (pr.author_login, pr.head_sha, pr.base_ref, pr.html_url) == (
        "dev",
        "h" * 40,
        "main",
        MR["web_url"],
    )
    assert ReviewRun.objects.get().trigger == "webhook"
    assert hook(anon, "Merge Request Hook", mr_event("open"), uuid_=delivery).json()["status"] == "duplicate"


@pytest.mark.django_db
def test_new_commits_queue_push_review(anon, enabled_repo, enqueued):
    hook(anon, "Merge Request Hook", mr_event("open"))
    event = mr_event("update", oldrev="o" * 40, last_commit={"id": "n" * 40})
    event["user"] = {"id": 8, "username": "someone-else"}
    response = hook(anon, "Merge Request Hook", event)
    assert response.json()["reason"].startswith("push_review_queued")
    assert ReviewRun.objects.filter(trigger="push", head_sha="n" * 40).exists()
    assert PullRequest.objects.get(number=7).author_login == "dev"  # updates do not change the author


@pytest.mark.django_db
def test_title_edits_do_not_review_but_ready_for_review_does(anon, enabled_repo, enqueued):
    response = hook(anon, "Merge Request Hook", mr_event("update", title="Renamed"))
    assert response.json()["reason"] == "merge_request_update" and ReviewRun.objects.count() == 0
    assert PullRequest.objects.get(number=7).title == "Renamed"
    ready = mr_event("update")
    ready["changes"] = {"draft": {"previous": True, "current": False}}
    assert hook(anon, "Merge Request Hook", ready).json()["reason"] == "review_queued"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (mr_event("open", draft=True), "draft_pull_request"),
        (mr_event("open", target_branch="release"), "review_queued"),
        (mr_event("merge", state="merged"), "merge_request_merge"),
        ({"object_kind": "pipeline"}, "unsupported_event:Pipeline Hook"),
    ],
)
def test_other_merge_request_events(anon, enabled_repo, enqueued, event, reason):
    name = "Pipeline Hook" if event.get("object_kind") == "pipeline" else "Merge Request Hook"
    assert hook(anon, name, event).json()["reason"] == reason


@pytest.mark.django_db
def test_disabled_repository_is_ignored(anon, gitlab_repo, enqueued):
    assert hook(anon, "Merge Request Hook", mr_event("open")).json()["reason"] == "repository_disabled"


def note_event(text: str, user_id: int = 7) -> dict:
    return {
        "object_kind": "note",
        "user": {"id": user_id, "username": "dev"},
        "project": {"id": 1001, "path_with_namespace": "acme/app"},
        "object_attributes": {"note": text, "noteable_type": "MergeRequest", "system": False},
        "merge_request": {
            "iid": 7,
            "title": "Add things",
            "url": MR["web_url"],
            "last_commit": {"id": "h" * 40},
        },
    }


@pytest.mark.django_db
def test_comment_commands_check_membership(anon, enabled_repo, enqueued, gitlab_api):
    gitlab_api.add("GET", r"/members/all/7$", {"access_level": 30})
    gitlab_api.add("GET", r"/members/all/9$", {"access_level": 20})
    assert hook(anon, "Note Hook", note_event("/reviewbot review")).json()["reason"] == "review_queued"
    assert ReviewRun.objects.get().trigger == "command"
    assert (
        hook(anon, "Note Hook", note_event("/reviewbot ignore", user_id=9)).json()["reason"]
        == "insufficient_permission"
    )
    assert hook(anon, "Note Hook", note_event("/reviewbot ignore")).json()["reason"] == "reviews_paused"
    assert PullRequest.objects.get(number=7).reviews_paused
    assert (
        hook(anon, "Note Hook", note_event("/reviewbot review", user_id=42)).json()["reason"]
        == "comment_from_bot"
    )
    assert hook(anon, "Note Hook", note_event("looks good")).json()["reason"] == "no_command"


# --- pipeline with GitLab-style partial inline rejection -----------------------------------------


@pytest.mark.django_db
def test_rejected_inline_comments_are_marked_in_summary(fake_credential):
    from tests.factories import make_repository

    repo = make_repository(credential=fake_credential, name="app")
    git = FakeGitProvider(reject_lines={("src/a.py", 3)})
    git.add_pull_request("acme/app", pr_info(1), [added_file("src/a.py", [f"x{i} = {i}" for i in range(10)])])
    pr = make_pull_request(repo, 1)
    run = create_run(pr, trigger=ReviewRun.Trigger.MANUAL, head_sha="a" * 40)
    script = [
        {
            "summary": "",
            "findings": [finding("src/a.py", 3, title="One"), finding("src/a.py", 5, title="Two")],
        }
    ]
    pipeline.execute(run.pk, git=git, llm=ScriptedLLM(script))
    statuses = dict(Finding.objects.values_list("title", "post_status"))
    assert statuses == {"One": "in_summary", "Two": "posted"}
