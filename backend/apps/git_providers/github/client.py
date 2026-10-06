"""GitHub REST client authenticated as a GitHub App installation."""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx2
import jwt
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

API_VERSION = "2022-11-28"
MAX_RATE_LIMIT_SLEEP = 60.0
FILES_PER_PAGE = 100
GITHUB_MAX_FILES = 3000  # GitHub's hard limit for the pull request files endpoint.
MAX_CHECK_TITLE = 255
MAX_CHECK_SUMMARY = 60_000  # GitHub limit is 65,535 characters.

# Installation tokens live only in process memory (never in the database, cache, or logs).
_token_cache: dict[tuple[str, int], tuple[str, float]] = {}
_token_lock = threading.Lock()


def app_jwt(app_id: str, private_key: str) -> str:
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": str(app_id)}
    return jwt.encode(payload, private_key, algorithm="RS256")


def clear_token_cache() -> None:
    with _token_lock:
        _token_cache.clear()


class GitHubAPI:
    """Low-level HTTP helper with GitHub rate-limit handling."""

    def __init__(self, api_url: str, http_client: httpx2.Client | None = None):
        self.api_url = api_url.rstrip("/")
        self.http = http_client or httpx2.Client(timeout=settings.GIT_TIMEOUT_SECONDS)

    def request(
        self,
        method: str,
        path: str,
        *,
        auth: str | None,
        json: Any = None,
        params: dict[str, Any] | None = None,
        attempts: int = 3,
        accept: str = "application/vnd.github+json",
    ) -> httpx2.Response:
        headers = {"Accept": accept, "X-GitHub-Api-Version": API_VERSION}
        if auth:
            headers["Authorization"] = auth
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        for attempt in range(attempts):
            try:
                response = self.http.request(method, url, headers=headers, json=json, params=params)
            except httpx2.HTTPError as exc:
                if attempt + 1 < attempts:
                    time.sleep(2**attempt)
                    continue
                raise GitProviderError(f"GitHub request failed: {type(exc).__name__}", status=None) from exc
            wait = self._rate_limit_wait(response)
            if wait is not None:
                if wait <= MAX_RATE_LIMIT_SLEEP and attempt + 1 < attempts:
                    logger.warning("github.rate_limited", wait_seconds=round(wait, 1), path=path)
                    time.sleep(wait)
                    continue
                raise GitProviderError(
                    "GitHub rate limit exceeded", status=response.status_code, retry_after=wait
                )
            if response.status_code >= 500 and attempt + 1 < attempts:
                time.sleep(2**attempt)
                continue
            if response.status_code >= 400:
                raise GitProviderError(
                    f"GitHub API {method} {path} failed ({response.status_code}): {_error_message(response)}",
                    status=response.status_code,
                )
            return response
        raise GitProviderError("GitHub request failed after retries")  # pragma: no cover

    @staticmethod
    def _rate_limit_wait(response: httpx2.Response) -> float | None:
        if response.status_code not in (403, 429):
            return None
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            try:
                return max(float(retry_after), 1.0)
            except ValueError:
                return 60.0
        if response.headers.get("x-ratelimit-remaining") == "0":
            reset = float(response.headers.get("x-ratelimit-reset", time.time() + 60))
            return max(reset - time.time(), 1.0)
        return None


def _error_message(response: httpx2.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    message = data.get("message", "") if isinstance(data, dict) else ""
    errors = data.get("errors") if isinstance(data, dict) else None
    if errors:
        message = f"{message} {errors}"
    return str(message)[:300]


class GitHubAppClient:
    """Calls made as the App itself (JWT auth)."""

    def __init__(
        self, *, api_url: str, app_id: str, private_key: str, http_client: httpx2.Client | None = None
    ):
        self.app_id = app_id
        self.private_key = private_key
        self.api = GitHubAPI(api_url, http_client)

    def _auth(self) -> str:
        return f"Bearer {app_jwt(self.app_id, self.private_key)}"

    def get_installation(self, installation_id: int) -> dict[str, Any]:
        return self.api.request("GET", f"/app/installations/{installation_id}", auth=self._auth()).json()

    def list_installations(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self.api.request(
                "GET", "/app/installations", auth=self._auth(), params={"per_page": 100, "page": page}
            ).json()
            results.extend(batch)
            if len(batch) < 100:
                return results
            page += 1

    def installation_token(self, installation_id: int) -> str:
        key = (self.app_id, installation_id)
        with _token_lock:
            cached = _token_cache.get(key)
            if cached and cached[1] > time.time() + 300:
                return cached[0]
        data = self.api.request(
            "POST", f"/app/installations/{installation_id}/access_tokens", auth=self._auth()
        ).json()
        token = data["token"]
        # Tokens are valid for 60 minutes; refresh after 50.
        with _token_lock:
            _token_cache[key] = (token, time.time() + 50 * 60)
        return token

    def for_installation(self, installation_id: int) -> GitHubInstallationClient:
        return GitHubInstallationClient(self, installation_id)


class GitHubInstallationClient:
    """Implements :class:`apps.git_providers.base.GitProvider` for one installation."""

    def __init__(self, app: GitHubAppClient, installation_id: int):
        self.app = app
        self.installation_id = installation_id
        self.api = app.api

    def _auth(self) -> str:
        return f"token {self.app.installation_token(self.installation_id)}"

    def list_repositories(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.api.request(
                "GET", "/installation/repositories", auth=self._auth(), params={"per_page": 100, "page": page}
            ).json()
            repos = data.get("repositories", [])
            results.extend(repos)
            if len(repos) < 100:
                return results
            page += 1

    def get_pull_request(self, repo_full_name: str, number: int) -> PullRequestInfo:
        data = self.api.request("GET", f"/repos/{repo_full_name}/pulls/{number}", auth=self._auth()).json()
        return pull_request_from_payload(data)

    def list_files(self, repo_full_name: str, number: int, max_files: int) -> list[ChangedFile]:
        files: list[ChangedFile] = []
        limit = min(max_files, GITHUB_MAX_FILES)
        page = 1
        while len(files) < limit:
            batch = self.api.request(
                "GET",
                f"/repos/{repo_full_name}/pulls/{number}/files",
                auth=self._auth(),
                params={"per_page": FILES_PER_PAGE, "page": page},
            ).json()
            for item in batch:
                files.append(_changed_file(item))
            if len(batch) < FILES_PER_PAGE:
                break
            page += 1
        return files[:limit] if len(files) > limit else files

    def post_review(
        self,
        repo_full_name: str,
        number: int,
        *,
        commit_sha: str,
        body: str,
        comments: list[InlineComment],
    ) -> PostedReview:
        payload: dict[str, Any] = {
            "commit_id": commit_sha,
            "body": body,
            "event": "COMMENT",
            "comments": [_inline_payload(c) for c in comments],
        }
        path = f"/repos/{repo_full_name}/pulls/{number}/reviews"
        try:
            data = self.api.request("POST", path, auth=self._auth(), json=payload).json()
        except GitProviderError as exc:
            if exc.status != 422 or not comments:
                raise
            # GitHub rejects the whole review if any inline position is invalid. The caller retries
            # body-only and moves the inline findings into the summary.
            logger.warning("github.inline_comments_rejected", error=str(exc))
            raise InlineCommentsRejected(str(exc)) from exc
        review = PostedReview(id=str(data["id"]), html_url=data.get("html_url", ""))
        if comments:
            review.comments = self._review_comment_urls(repo_full_name, number, data["id"])
        return review

    def _review_comment_urls(
        self, repo_full_name: str, number: int, review_id: int
    ) -> dict[tuple[str, int], tuple[str, str]]:
        try:
            items = self.api.request(
                "GET",
                f"/repos/{repo_full_name}/pulls/{number}/reviews/{review_id}/comments",
                auth=self._auth(),
                params={"per_page": 100},
            ).json()
        except GitProviderError as exc:
            logger.warning("github.review_comments_lookup_failed", error=str(exc))
            return {}
        out: dict[tuple[str, int], tuple[str, str]] = {}
        for item in items:
            line = item.get("line") or item.get("original_line")
            if line is not None:
                out[(item["path"], int(line))] = (str(item["id"]), item.get("html_url", ""))
        return out

    def get_file(self, repo_full_name: str, path: str, ref: str) -> str | None:
        """Raw file contents at ``ref``, or None when the file does not exist."""
        try:
            response = self.api.request(
                "GET",
                f"/repos/{repo_full_name}/contents/{path}",
                auth=self._auth(),
                params={"ref": ref},
                accept="application/vnd.github.raw+json",
            )
        except GitProviderError as exc:
            if exc.status == 404:
                return None
            raise
        return response.text

    def compare(self, repo_full_name: str, base: str, head: str) -> CompareResult:
        """Files changed between two commits. GitHub returns at most 300 files for a comparison."""
        data = self.api.request(
            "GET", f"/repos/{repo_full_name}/compare/{base}...{head}", auth=self._auth()
        ).json()
        return CompareResult(
            status=data.get("status", "diverged"),
            files=[_changed_file(item) for item in data.get("files") or []],
        )

    def create_check_run(self, repo_full_name: str, head_sha: str, *, name: str, details_url: str) -> str:
        payload = {
            "name": name,
            "head_sha": head_sha,
            "status": "in_progress",
            "details_url": details_url,
            "output": {"title": "Review in progress", "summary": "Reviewbot is reviewing this commit."},
        }
        data = self.api.request(
            "POST", f"/repos/{repo_full_name}/check-runs", auth=self._auth(), json=payload
        )
        return str(data.json()["id"])

    def complete_check_run(
        self, repo_full_name: str, check_run_id: str, *, conclusion: str, title: str, summary: str
    ) -> None:
        payload = {
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title[:MAX_CHECK_TITLE], "summary": summary[:MAX_CHECK_SUMMARY]},
        }
        self.api.request(
            "PATCH", f"/repos/{repo_full_name}/check-runs/{check_run_id}", auth=self._auth(), json=payload
        )


def _changed_file(item: dict[str, Any]) -> ChangedFile:
    return ChangedFile(
        path=item["filename"],
        status=item.get("status", "modified"),
        additions=int(item.get("additions", 0)),
        deletions=int(item.get("deletions", 0)),
        patch=item.get("patch"),
        previous_path=item.get("previous_filename"),
    )


class InlineCommentsRejected(GitProviderError):
    def __init__(self, message: str):
        super().__init__(message, status=422)


def _inline_payload(comment: InlineComment) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": comment.path,
        "line": comment.line,
        "side": "RIGHT",
        "body": comment.body,
    }
    if comment.start_line is not None and comment.start_line < comment.line:
        payload["start_line"] = comment.start_line
        payload["start_side"] = "RIGHT"
    return payload


def pull_request_from_payload(data: dict[str, Any]) -> PullRequestInfo:
    if data.get("merged") or data.get("merged_at"):
        state = "merged"
    else:
        state = "closed" if data.get("state") == "closed" else "open"
    return PullRequestInfo(
        number=int(data["number"]),
        title=str(data.get("title", ""))[:500],
        author_login=(data.get("user") or {}).get("login", ""),
        state=state,
        is_draft=bool(data.get("draft", False)),
        base_ref=(data.get("base") or {}).get("ref", ""),
        head_ref=(data.get("head") or {}).get("ref", ""),
        base_sha=(data.get("base") or {}).get("sha", ""),
        head_sha=(data.get("head") or {}).get("sha", ""),
        html_url=data.get("html_url", ""),
    )


def exchange_manifest_code(
    api_url: str, code: str, http_client: httpx2.Client | None = None
) -> dict[str, Any]:
    """Completes the GitHub App manifest flow and returns the new App's credentials."""
    api = GitHubAPI(api_url, http_client)
    return api.request("POST", f"/app-manifests/{code}/conversions", auth=None).json()
