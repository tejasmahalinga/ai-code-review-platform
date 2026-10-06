"""In-memory GitProvider used by tests and the end-to-end smoke test."""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.git_providers.base import (
    ChangedFile,
    CompareResult,
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
class FakeCheckRun:
    id: str
    repo: str
    head_sha: str
    name: str
    details_url: str
    status: str = "in_progress"
    conclusion: str | None = None
    title: str = ""
    summary: str = ""


@dataclass
class FakeGitProvider:
    pull_requests: dict[tuple[str, int], PullRequestInfo] = field(default_factory=dict)
    files: dict[tuple[str, int], list[ChangedFile]] = field(default_factory=dict)
    posted: list[PostedCall] = field(default_factory=list)
    reject_inline: bool = False
    reject_lines: set[tuple[str, int]] = field(default_factory=set)  # refused one by one (GitLab style)
    fail_with: GitProviderError | None = None
    comparisons: dict[tuple[str, str, str], CompareResult] = field(default_factory=dict)
    compare_calls: list[tuple[str, str, str]] = field(default_factory=list)
    check_runs: list[FakeCheckRun] = field(default_factory=list)
    checks_forbidden: bool = False
    contents: dict[tuple[str, str, str], str] = field(default_factory=dict)  # (repo, path, ref) -> text
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
        from apps.git_providers.base import InlineCommentsRejected

        if self.reject_inline and comments:
            raise InlineCommentsRejected("Unprocessable Entity: pull_request_review_thread.line")
        self.posted.append(PostedCall(repo_full_name, number, commit_sha, body, list(comments)))
        review_id = self._next_id
        self._next_id += 1
        base = f"https://github.example/{repo_full_name}/pull/{number}"
        review = PostedReview(id=str(review_id), html_url=f"{base}#pullrequestreview-{review_id}")
        for index, comment in enumerate(comments):
            if (comment.path, comment.line) in self.reject_lines:
                review.rejected.add((comment.path, comment.line))
                continue
            comment_id = f"{review_id}{index:03d}"
            review.comments[(comment.path, comment.line)] = (comment_id, f"{base}#discussion_r{comment_id}")
        return review

    def get_file(self, repo_full_name: str, path: str, ref: str) -> str | None:
        return self.contents.get((repo_full_name, path, ref))

    def add_comparison(self, repo: str, base: str, head: str, result: CompareResult) -> None:
        self.comparisons[(repo, base, head)] = result

    def compare(self, repo_full_name: str, base: str, head: str) -> CompareResult:
        self.compare_calls.append((repo_full_name, base, head))
        try:
            return self.comparisons[(repo_full_name, base, head)]
        except KeyError as exc:
            raise GitProviderError("Not Found", status=404) from exc

    def create_check_run(self, repo_full_name: str, head_sha: str, *, name: str, details_url: str) -> str:
        if self.checks_forbidden:
            raise GitProviderError("Resource not accessible by integration", status=403)
        check = FakeCheckRun(str(len(self.check_runs) + 1), repo_full_name, head_sha, name, details_url)
        self.check_runs.append(check)
        return check.id

    def complete_check_run(
        self, repo_full_name: str, check_run_id: str, *, conclusion: str, title: str, summary: str
    ) -> None:
        check = next(c for c in self.check_runs if c.id == check_run_id)
        check.status, check.conclusion, check.title, check.summary = "completed", conclusion, title, summary
