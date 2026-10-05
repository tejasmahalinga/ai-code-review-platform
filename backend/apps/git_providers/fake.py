"""In-memory GitProvider used by tests and the end-to-end smoke test."""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.git_providers.base import (
    ChangedFile,
    GitProviderError,
    InlineComment,
    PostedReview,
    PullRequestInfo,
)


@dataclass
class PostedCall:
    repo: str
    number: int
    commit_sha: str
    body: str
    comments: list[InlineComment]


@dataclass
class FakeGitProvider:
    pull_requests: dict[tuple[str, int], PullRequestInfo] = field(default_factory=dict)
    files: dict[tuple[str, int], list[ChangedFile]] = field(default_factory=dict)
    posted: list[PostedCall] = field(default_factory=list)
    reject_inline: bool = False
    fail_with: GitProviderError | None = None
    _next_id: int = 1000

    def add_pull_request(self, repo: str, info: PullRequestInfo, files: list[ChangedFile]) -> None:
        self.pull_requests[(repo, info.number)] = info
        self.files[(repo, info.number)] = files

    def get_pull_request(self, repo_full_name: str, number: int) -> PullRequestInfo:
        if self.fail_with:
            raise self.fail_with
        try:
            return self.pull_requests[(repo_full_name, number)]
        except KeyError as exc:
            raise GitProviderError("Not Found", status=404) from exc

    def list_files(self, repo_full_name: str, number: int, max_files: int) -> list[ChangedFile]:
        return self.files.get((repo_full_name, number), [])[:max_files]

    def post_review(
        self,
        repo_full_name: str,
        number: int,
        *,
        commit_sha: str,
        body: str,
        comments: list[InlineComment],
    ) -> PostedReview:
        from apps.git_providers.github.client import InlineCommentsRejected

        if self.reject_inline and comments:
            raise InlineCommentsRejected("Unprocessable Entity: pull_request_review_thread.line")
        self.posted.append(PostedCall(repo_full_name, number, commit_sha, body, list(comments)))
        review_id = self._next_id
        self._next_id += 1
        base = f"https://github.example/{repo_full_name}/pull/{number}"
        review = PostedReview(id=str(review_id), html_url=f"{base}#pullrequestreview-{review_id}")
        for index, comment in enumerate(comments):
            comment_id = f"{review_id}{index:03d}"
            review.comments[(comment.path, comment.line)] = (comment_id, f"{base}#discussion_r{comment_id}")
        return review
