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


class GitProviderError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.retry_after is not None or (self.status is not None and self.status >= 500)


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
