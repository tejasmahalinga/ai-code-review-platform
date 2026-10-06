"""Provider-neutral Git hosting interface.

The review engine only talks to :class:`GitProvider`, so adding GitLab or Bitbucket (INT-03/INT-08)
means implementing this protocol, not touching the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class PullRequestInfo:
    number: int
    title: str
    author_login: str
    state: str  # open | closed | merged
    is_draft: bool
    base_ref: str
    head_ref: str
    base_sha: str
    head_sha: str
    html_url: str


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str  # added | modified | removed | renamed | copied | changed | unchanged
    additions: int
    deletions: int
    patch: str | None  # None for binary files or diffs too large for the provider to return
    previous_path: str | None = None


@dataclass(frozen=True)
class InlineComment:
    path: str
    line: int
    body: str
    start_line: int | None = None


@dataclass
class PostedReview:
    id: str
    html_url: str
    # (path, line) -> (comment id, comment url), best effort.
    comments: dict[tuple[str, int], tuple[str, str]] = field(default_factory=dict)
    inline_rejected: bool = False
    # Inline comments the provider refused individually (path, line); the provider put them in the summary.
    rejected: set[tuple[str, int]] = field(default_factory=set)


@dataclass(frozen=True)
class CompareResult:
    status: str  # ahead | behind | diverged | identical
    files: list[ChangedFile]


CHECK_CONCLUSIONS = {"success", "failure", "neutral", "skipped", "cancelled"}


class GitProviderError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.retry_after is not None or (self.status is not None and self.status >= 500)


class InlineCommentsRejected(GitProviderError):
    """The provider refused the inline comments; the caller re-posts with them in the summary."""

    def __init__(self, message: str):
        super().__init__(message, status=422)


class GitProvider(Protocol):
    def get_pull_request(self, repo_full_name: str, number: int) -> PullRequestInfo: ...

    def list_files(self, repo_full_name: str, number: int, max_files: int) -> list[ChangedFile]: ...

    def post_review(
        self,
        repo_full_name: str,
        number: int,
        *,
        commit_sha: str,
        body: str,
        comments: list[InlineComment],
    ) -> PostedReview: ...

    def get_file(self, repo_full_name: str, path: str, ref: str) -> str | None: ...

    def compare(self, repo_full_name: str, base: str, head: str) -> CompareResult: ...

    def create_check_run(self, repo_full_name: str, head_sha: str, *, name: str, details_url: str) -> str: ...

    def complete_check_run(
        self, repo_full_name: str, check_run_id: str, *, conclusion: str, title: str, summary: str
    ) -> None: ...
