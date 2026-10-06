"""GitLab REST (v4) client implementing :class:`apps.git_providers.base.GitProvider` (INT-03).

Reviewbot acts as one GitLab user (ideally a dedicated bot account) through an access token with the ``api``
scope. Merge requests map onto the provider-neutral pull request model: ``number`` is the MR ``iid`` and
``repo_full_name`` is the project's ``path_with_namespace``.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote

import httpx2
from django.conf import settings

from apps.core.logging import get_logger
from apps.git_providers.base import (
    ChangedFile,
    CompareResult,
    GitProviderError,
    InlineComment,
    PostedReview,
    PullRequestInfo,
)

logger = get_logger(__name__)

PER_PAGE = 100
MAX_RATE_LIMIT_SLEEP = 60.0
MAX_STATUS_DESCRIPTION = 255
DEVELOPER = 30
MAINTAINER = 40

# GitHub-style check conclusions mapped to GitLab commit status states. Only a gate failure fails the status:
# a "failed" external status can block merges when "Pipelines must succeed" is on.
STATUS_STATES = {
    "success": "success",
    "neutral": "success",
    "skipped": "success",
    "failure": "failed",
    "cancelled": "canceled",
}
_LINK = re.compile(r"\((https?://[^)\s]+)\)")


def project_path(repo_full_name: str) -> str:
    return quote(repo_full_name, safe="")


class GitLabAPI:
    def __init__(self, api_url: str, token: str, http_client: httpx2.Client | None = None):
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.http = http_client or httpx2.Client(timeout=settings.GIT_TIMEOUT_SECONDS)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        attempts: int = 3,
    ) -> httpx2.Response:
        headers = {"PRIVATE-TOKEN": self.token, "Accept": "application/json"}
        url = f"{self.api_url}{path}"
        for attempt in range(attempts):
            try:
                response = self.http.request(method, url, headers=headers, json=json, params=params)
            except httpx2.HTTPError as exc:
                if attempt + 1 < attempts:
                    time.sleep(2**attempt)
                    continue
                raise GitProviderError(f"GitLab request failed: {type(exc).__name__}", status=None) from exc
            if response.status_code == 429:
                wait = _retry_after(response)
                if wait <= MAX_RATE_LIMIT_SLEEP and attempt + 1 < attempts:
                    logger.warning("gitlab.rate_limited", wait_seconds=round(wait, 1), path=path)
                    time.sleep(wait)
                    continue
                raise GitProviderError("GitLab rate limit exceeded", status=429, retry_after=wait)
            if response.status_code >= 500 and attempt + 1 < attempts:
                time.sleep(2**attempt)
                continue
            if response.status_code >= 400:
                raise GitProviderError(
                    f"GitLab API {method} {path} failed ({response.status_code}): {_error_message(response)}",
                    status=response.status_code,
                )
            return response
        raise GitProviderError("GitLab request failed after retries")  # pragma: no cover

    def paginate(self, path: str, params: dict[str, Any] | None = None, limit: int = 10_000) -> list[Any]:
        results: list[Any] = []
        page = 1
        while len(results) < limit:
            response = self.request(
                "GET", path, params={**(params or {}), "per_page": PER_PAGE, "page": page}
            )
            batch = response.json()
            results.extend(batch)
            next_page = response.headers.get("x-next-page", "")
            if not batch or (next_page == "" and len(batch) < PER_PAGE):
                break
            page = int(next_page) if next_page.isdigit() else page + 1
        return results[:limit]


def _retry_after(response: httpx2.Response) -> float:
    for header in ("retry-after", "ratelimit-reset"):
        value = response.headers.get(header)
        if value and value.isdigit():
            number = float(value)
            # RateLimit-Reset is an epoch timestamp; Retry-After is seconds.
            return max(number - time.time(), 1.0) if number > 1_000_000_000 else max(number, 1.0)
    return 60.0


def _error_message(response: httpx2.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or data)[:300]
    return str(data)[:300]


def _count_changes(diff: str) -> tuple[int, int]:
    additions = deletions = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return additions, deletions


def changed_file(item: dict[str, Any]) -> ChangedFile:
    diff = item.get("diff") or ""
    patch = diff if diff.startswith("@@") else None  # binary, too large, or collapsed
    additions, deletions = _count_changes(diff) if patch else (0, 0)
    if item.get("new_file"):
        status = "added"
    elif item.get("deleted_file"):
        status = "removed"
    elif item.get("renamed_file"):
        status = "renamed"
    else:
        status = "modified"
    return ChangedFile(
        path=item.get("new_path") or item.get("old_path", ""),
        status=status,
        additions=additions,
        deletions=deletions,
        patch=patch,
        previous_path=item.get("old_path") if item.get("renamed_file") else None,
    )


def merge_request_from_payload(data: dict[str, Any]) -> PullRequestInfo:
    state = {"opened": "open", "merged": "merged"}.get(str(data.get("state")), "closed")
    refs = data.get("diff_refs") or {}
    return PullRequestInfo(
        number=int(data["iid"]),
        title=str(data.get("title", ""))[:500],
        author_login=(data.get("author") or {}).get("username", ""),
        state=state,
        is_draft=bool(data.get("draft") or data.get("work_in_progress")),
        base_ref=str(data.get("target_branch", "")),
        head_ref=str(data.get("source_branch", "")),
        base_sha=str(refs.get("base_sha") or ""),
        head_sha=str(data.get("sha") or refs.get("head_sha") or ""),
        html_url=str(data.get("web_url", "")),
    )


def gitlab_suggestion(body: str, comment: InlineComment) -> str:
    """GitLab suggestions say how many lines above the commented line they replace (```suggestion:-N+0)."""
    above = (
        comment.line - comment.start_line if comment.start_line and comment.start_line < comment.line else 0
    )
    return body.replace("```suggestion\n", f"```suggestion:-{above}+0\n")


class GitLabClient:
    def __init__(self, api_url: str, token: str, http_client: httpx2.Client | None = None):
        self.api = GitLabAPI(api_url, token, http_client)

    # --- connection and repositories ---------------------------------------------------------------

    def current_user(self) -> dict[str, Any]:
        return self.api.request("GET", "/user").json()

    def token_scopes(self) -> list[str] | None:
        """Scopes of the token (GitLab 15.5+). None when the instance cannot tell."""
        try:
            data = self.api.request("GET", "/personal_access_tokens/self").json()
        except GitProviderError as exc:
            if exc.status in (401, 403, 404):
                return None
            raise
        return [str(s) for s in data.get("scopes", [])]

    def list_projects(self) -> list[dict[str, Any]]:
        """Projects the bot can review: Developer access or more (needed for commit statuses)."""
        return self.api.paginate(
            "/projects",
            {"membership": "true", "min_access_level": DEVELOPER, "archived": "false", "simple": "true"},
        )

    def create_project_hook(self, repo_full_name: str, url: str, token: str) -> str:
        payload = {
            "url": url,
            "token": token,
            "merge_requests_events": True,
            "note_events": True,
            "push_events": False,
            "enable_ssl_verification": url.startswith("https://"),
        }
        data = self.api.request(
            "POST", f"/projects/{project_path(repo_full_name)}/hooks", json=payload
        ).json()
        return str(data["id"])

    def hook_exists(self, repo_full_name: str, hook_id: str) -> bool:
        try:
            self.api.request("GET", f"/projects/{project_path(repo_full_name)}/hooks/{hook_id}")
        except GitProviderError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def delete_project_hook(self, repo_full_name: str, hook_id: str) -> None:
        try:
            self.api.request("DELETE", f"/projects/{project_path(repo_full_name)}/hooks/{hook_id}")
        except GitProviderError as exc:
            if exc.status != 404:
                raise

    def member_access_level(self, repo_full_name: str, user_id: int) -> int:
        """The user's effective access level on the project (inherited from groups too); 0 if none."""
        try:
            data = self.api.request(
                "GET", f"/projects/{project_path(repo_full_name)}/members/all/{int(user_id)}"
            ).json()
        except GitProviderError as exc:
            if exc.status == 404:
                return 0
            raise
        return int(data.get("access_level") or 0)

    # --- GitProvider -------------------------------------------------------------------------------

    def _mr_path(self, repo_full_name: str, number: int) -> str:
        return f"/projects/{project_path(repo_full_name)}/merge_requests/{int(number)}"

    def get_pull_request(self, repo_full_name: str, number: int) -> PullRequestInfo:
        data = self.api.request("GET", self._mr_path(repo_full_name, number)).json()
        if data.get("state") == "opened" and not (data.get("diff_refs") or {}).get("head_sha"):
            # GitLab computes the diff asynchronously right after a push; review once it is ready.
            raise GitProviderError("GitLab is still preparing the merge request diff", retry_after=15)
        return merge_request_from_payload(data)

    def list_files(self, repo_full_name: str, number: int, max_files: int) -> list[ChangedFile]:
        path = self._mr_path(repo_full_name, number)
        try:
            items = self.api.paginate(f"{path}/diffs", limit=max_files)
        except GitProviderError as exc:
            if exc.status != 404:
                raise
            # GitLab before 15.7 has no /diffs endpoint.
            items = (self.api.request("GET", f"{path}/changes").json().get("changes") or [])[:max_files]
        return [changed_file(item) for item in items]

    def _diff_refs(self, repo_full_name: str, number: int, commit_sha: str) -> tuple[dict[str, str], str]:
        """Position SHAs of the MR diff version for ``commit_sha`` (falls back to the latest version)."""
        path = self._mr_path(repo_full_name, number)
        mr = self.api.request("GET", path).json()
        refs = dict(mr.get("diff_refs") or {})
        try:
            versions = self.api.request("GET", f"{path}/versions").json()
        except GitProviderError:
            versions = []
        for version in versions:
            if version.get("head_commit_sha") == commit_sha:
                refs = {
                    "base_sha": version["base_commit_sha"],
                    "start_sha": version["start_commit_sha"],
                    "head_sha": version["head_commit_sha"],
                }
                break
        return refs, str(mr.get("web_url", ""))

    def post_review(
        self,
        repo_full_name: str,
        number: int,
        *,
        commit_sha: str,
        body: str,
        comments: list[InlineComment],
    ) -> PostedReview:
        """Posts each inline comment as a diff discussion, then the summary as an MR note.

        GitLab has no atomic multi-comment review: a comment whose position GitLab refuses is added to the
        summary instead and reported in ``PostedReview.rejected``.
        """
        path = self._mr_path(repo_full_name, number)
        refs, web_url = self._diff_refs(repo_full_name, number, commit_sha)
        placed: dict[tuple[str, int], tuple[str, str]] = {}
        rejected: list[InlineComment] = []
        for comment in comments:
            payload = {
                "body": gitlab_suggestion(comment.body, comment),
                "position": {
                    "position_type": "text",
                    "base_sha": refs.get("base_sha"),
                    "start_sha": refs.get("start_sha"),
                    "head_sha": refs.get("head_sha") or commit_sha,
                    "old_path": comment.path,
                    "new_path": comment.path,
                    "new_line": comment.line,
                },
            }
            try:
                data = self.api.request("POST", f"{path}/discussions", json=payload).json()
            except GitProviderError as exc:
                if exc.status not in (400, 422):
                    raise
                logger.warning(
                    "gitlab.inline_comment_rejected", path=comment.path, line=comment.line, error=str(exc)
                )
                rejected.append(comment)
                continue
            note_id = str((data.get("notes") or [{}])[0].get("id", data.get("id", "")))
            placed[(comment.path, comment.line)] = (note_id, f"{web_url}#note_{note_id}")
        if rejected:
            extra = "\n\n".join(f"**`{c.path}` line {c.line}**\n\n{c.body}" for c in rejected)
            summary = "<summary>Comments that could not be placed on the diff</summary>"
            body = f"{body}\n\n<details>{summary}\n\n{extra}\n\n</details>"
        note = self.api.request("POST", f"{path}/notes", json={"body": body}).json()
        note_id = str(note["id"])
        return PostedReview(
            id=note_id,
            html_url=f"{web_url}#note_{note_id}",
            comments=placed,
            rejected={(c.path, c.line) for c in rejected},
        )

    def get_file(self, repo_full_name: str, path: str, ref: str) -> str | None:
        try:
            response = self.api.request(
                "GET",
                f"/projects/{project_path(repo_full_name)}/repository/files/{quote(path, safe='')}/raw",
                params={"ref": ref},
            )
        except GitProviderError as exc:
            if exc.status == 404:
                return None
            raise
        return response.text

    def compare(self, repo_full_name: str, base: str, head: str) -> CompareResult:
        project = project_path(repo_full_name)
        if base == head:
            return CompareResult(status="identical", files=[])
        merge_base = self.api.request(
            "GET", f"/projects/{project}/repository/merge_base", params=[("refs[]", base), ("refs[]", head)]
        ).json()
        if merge_base.get("id") != base:
            return CompareResult(status="diverged", files=[])  # force push: base is not an ancestor
        data = self.api.request(
            "GET",
            f"/projects/{project}/repository/compare",
            params={"from": base, "to": head, "straight": "true"},
        ).json()
        return CompareResult(status="ahead", files=[changed_file(d) for d in data.get("diffs") or []])

    def create_check_run(self, repo_full_name: str, head_sha: str, *, name: str, details_url: str) -> str:
        self._set_status(repo_full_name, head_sha, "running", name, details_url, "Review in progress")
        return f"{head_sha}:{name}"[:64]

    def complete_check_run(
        self, repo_full_name: str, check_run_id: str, *, conclusion: str, title: str, summary: str
    ) -> None:
        sha, _, name = check_run_id.partition(":")
        link = _LINK.search(summary)
        state = STATUS_STATES.get(conclusion, "success")
        self._set_status(
            repo_full_name, sha, state, name or "Reviewbot", link.group(1) if link else "", title
        )

    def _set_status(
        self, repo_full_name: str, sha: str, state: str, name: str, url: str, description: str
    ) -> None:
        payload: dict[str, Any] = {
            "state": state,
            "name": name,
            "description": description[:MAX_STATUS_DESCRIPTION],
        }
        if url:
            payload["target_url"] = url
        self.api.request("POST", f"/projects/{project_path(repo_full_name)}/statuses/{sha}", json=payload)


def client_for_connection(connection: Any, http_client: httpx2.Client | None = None) -> GitLabClient:
    return GitLabClient(connection.api_url, connection.access_token, http_client)
