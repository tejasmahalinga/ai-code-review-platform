"""Handlers for GitLab webhook events (merge requests and MR comments). Database work and enqueueing only.

Each handler returns ``(status, reason, repository, review_run)`` for the delivery log.
"""

from __future__ import annotations

from typing import Any

from apps.core.logging import get_logger
from apps.git_providers.base import GitProviderError, PullRequestInfo
from apps.git_providers.gitlab.client import DEVELOPER, client_for_connection
from apps.repositories import services as repo_services
from apps.repositories.models import GitProviderConnection, Repository
from apps.reviews.engine.repo_config import branch_matches
from apps.reviews.models import PullRequest, ReviewRun
from apps.reviews.services import (
    ActiveRunExists,
    DuplicateRun,
    create_run,
    request_review,
    upsert_pull_request,
)
from apps.webhooks.github_handlers import COMMANDS, IGNORED, PROCESSED, Result, _push, parse_command

logger = get_logger(__name__)

STATES = {"opened": "open", "merged": "merged", "closed": "closed", "locked": "closed"}


def handle(connection: GitProviderConnection, event: str, payload: dict[str, Any]) -> Result:
    kind = payload.get("object_kind")
    if event == "Merge Request Hook" or kind == "merge_request":
        return _merge_request(connection, payload)
    if event == "Note Hook" or kind == "note":
        return _note(connection, payload)
    return IGNORED, f"unsupported_event:{event or kind}", None, None


def _find_repository(connection: GitProviderConnection, payload: dict[str, Any]) -> Repository | None:
    project_id = int((payload.get("project") or {}).get("id") or 0)
    return (
        Repository.objects.select_related("installation__connection", "settings", "settings__credential")
        .filter(
            installation__connection=connection, installation__removed_at__isnull=True, external_id=project_id
        )
        .first()
    )


def _info(attrs: dict[str, Any], author: str) -> PullRequestInfo:
    return PullRequestInfo(
        number=int(attrs["iid"]),
        title=str(attrs.get("title", ""))[:500],
        author_login=author,
        state=STATES.get(str(attrs.get("state")), "closed"),
        is_draft=bool(attrs.get("draft") or attrs.get("work_in_progress")),
        base_ref=str(attrs.get("target_branch", "")),
        head_ref=str(attrs.get("source_branch", "")),
        base_sha="",  # not in the payload; the review reads it from the API
        head_sha=str((attrs.get("last_commit") or {}).get("id", "")),
        html_url=str(attrs.get("url", "")),
    )


def _merge_request(connection: GitProviderConnection, payload: dict[str, Any]) -> Result:
    attrs = payload.get("object_attributes") or {}
    action = str(attrs.get("action", ""))
    repository = _find_repository(connection, payload)
    if repository is None:
        return IGNORED, "unknown_repository", None, None
    if not repository.is_reviewable:
        return IGNORED, "repository_disabled", repository, None

    # The actor opened it, so they are the author; other events keep the stored author.
    author = (payload.get("user") or {}).get("username", "") if action == "open" else ""
    info = _info(attrs, author)
    pull_request = upsert_pull_request(repository, info)

    changes = payload.get("changes") or {}
    new_commits = action == "update" and bool(attrs.get("oldrev"))
    marked_ready = action == "update" and _became_ready(changes)
    if action not in ("open", "reopen") and not new_commits and not marked_ready:
        return PROCESSED, f"merge_request_{action or 'event'}", repository, None

    settings = repo_services.ensure_settings(repository)
    if not settings.auto_review:
        return IGNORED, "auto_review_disabled", repository, None
    if info.is_draft and not settings.review_drafts:
        return IGNORED, "draft_pull_request", repository, None
    if info.state != "open":
        return IGNORED, "pull_request_not_open", repository, None
    if not branch_matches(info.base_ref, list(settings.base_branch_patterns)):
        return IGNORED, "base_branch_filtered", repository, None
    if pull_request.reviews_paused:
        return IGNORED, "reviews_paused", repository, None
    if new_commits:
        return _push(repository, pull_request, info.head_sha, "", settings.review_on_push)
    try:
        run = create_run(pull_request, trigger=ReviewRun.Trigger.WEBHOOK, head_sha=info.head_sha)
    except DuplicateRun:
        return IGNORED, "already_reviewed_commit", repository, None
    return PROCESSED, "review_queued", repository, run


def _became_ready(changes: dict[str, Any]) -> bool:
    for key in ("draft", "work_in_progress"):
        change = changes.get(key) or {}
        if change.get("previous") is True and change.get("current") is False:
            return True
    return False


def _note(connection: GitProviderConnection, payload: dict[str, Any]) -> Result:
    attrs = payload.get("object_attributes") or {}
    if attrs.get("noteable_type") != "MergeRequest":
        return IGNORED, "not_a_merge_request", None, None
    if attrs.get("system"):
        return IGNORED, "system_note", None, None
    command = parse_command(str(attrs.get("note", "")))
    if command is None:
        return IGNORED, "no_command", None, None
    user = payload.get("user") or {}
    bot = (
        connection.installations.filter(removed_at__isnull=True).values_list("external_id", flat=True).first()
    )
    if user.get("id") is not None and int(user["id"]) == bot:
        return IGNORED, "comment_from_bot", None, None
    repository = _find_repository(connection, payload)
    if repository is None:
        return IGNORED, "unknown_repository", None, None
    if not repository.is_reviewable:
        return IGNORED, "repository_disabled", repository, None
    if command not in COMMANDS:
        return IGNORED, f"unknown_command:{command[:32]}", repository, None
    # Like GitHub owners/members/collaborators: only people who can push (Developer or higher) may command.
    try:
        level = client_for_connection(connection).member_access_level(
            repository.full_name, int(user.get("id") or 0)
        )
    except GitProviderError as exc:
        logger.warning("gitlab.member_lookup_failed", repo=repository.full_name, error=str(exc))
        level = 0
    if level < DEVELOPER:
        logger.info("webhook.command_rejected", repo=repository.full_name, access_level=level)
        return IGNORED, "insufficient_permission", repository, None

    mr = payload.get("merge_request") or {}
    pull_request, _ = PullRequest.objects.get_or_create(
        repository=repository,
        number=int(mr.get("iid") or 0),
        defaults={
            "title": str(mr.get("title", ""))[:500],
            "html_url": mr.get("url", ""),
            "head_sha": str((mr.get("last_commit") or {}).get("id", "")),
            "base_ref": mr.get("target_branch", ""),
            "head_ref": mr.get("source_branch", ""),
        },
    )
    if command in ("ignore", "resume"):
        pull_request.reviews_paused = command == "ignore"
        pull_request.save(update_fields=["reviews_paused"])
        return PROCESSED, f"reviews_{'paused' if command == 'ignore' else 'resumed'}", repository, None
    settings = repo_services.ensure_settings(repository)
    if not (settings.credential and settings.credential.is_usable):
        return IGNORED, "no_valid_llm_key", repository, None
    try:
        run = request_review(pull_request, trigger=ReviewRun.Trigger.COMMAND)
    except ActiveRunExists:
        return IGNORED, "review_already_running", repository, None
    return PROCESSED, "review_queued", repository, run
