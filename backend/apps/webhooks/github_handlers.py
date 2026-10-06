"""Handlers for GitHub webhook events. They only touch the database and enqueue work.

Each handler returns ``(status, reason, repository, review_run)`` for the delivery log.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings as django_settings

from apps.core.logging import get_logger
from apps.git_providers.github.client import pull_request_from_payload
from apps.repositories import services as repo_services
from apps.repositories.models import GitProviderConnection, Installation, Repository
from apps.reviews.engine.repo_config import branch_matches
from apps.reviews.models import PullRequest, ReviewRun
from apps.reviews.services import (
    ActiveRunExists,
    DuplicateRun,
    create_run,
    request_review,
    supersede_queued_push_runs,
    upsert_pull_request,
)
from apps.webhooks.models import WebhookDelivery

logger = get_logger(__name__)

Result = tuple[str, str, Repository | None, ReviewRun | None]
PROCESSED = WebhookDelivery.Status.PROCESSED
IGNORED = WebhookDelivery.Status.IGNORED

REVIEW_ACTIONS = {"opened", "reopened", "ready_for_review"}
TRACKED_ACTIONS = REVIEW_ACTIONS | {"synchronize", "closed", "edited", "converted_to_draft"}


def handle(connection: GitProviderConnection, event: str, payload: dict[str, Any]) -> Result:
    if event == "ping":
        return PROCESSED, "pong", None, None
    if event == "installation":
        return _installation(connection, payload)
    if event == "installation_repositories":
        return _installation_repositories(connection, payload)
    if event == "pull_request":
        return _pull_request(connection, payload)
    if event == "issue_comment":
        return _issue_comment(connection, payload)
    return IGNORED, f"unsupported_event:{event}", None, None


def _installation(connection: GitProviderConnection, payload: dict[str, Any]) -> Result:
    action = payload.get("action")
    data = payload["installation"]
    if action == "deleted":
        installation = Installation.objects.filter(connection=connection, external_id=data["id"]).first()
        if installation:
            repo_services.remove_installation(installation)
        return PROCESSED, "installation_deleted", None, None
    installation = repo_services.upsert_installation(connection, data)
    if action == "suspend":
        return PROCESSED, "installation_suspended", None, None
    if action == "created":
        repo_services.sync_installation_repositories(installation, payload.get("repositories") or [])
    return PROCESSED, f"installation_{action}", None, None


def _installation_repositories(connection: GitProviderConnection, payload: dict[str, Any]) -> Result:
    installation = repo_services.upsert_installation(connection, payload["installation"])
    for repo in payload.get("repositories_added") or []:
        repo_services.upsert_repository(installation, repo)
    removed = [int(r["id"]) for r in payload.get("repositories_removed") or []]
    if removed:
        repo_services.mark_repositories_removed(installation, removed)
    return PROCESSED, "repositories_synced", None, None


def _pull_request(connection: GitProviderConnection, payload: dict[str, Any]) -> Result:
    action = payload.get("action", "")
    if action not in TRACKED_ACTIONS:
        return IGNORED, f"unsupported_action:{action}", None, None
    repository = _find_repository(connection, payload)
    if repository is None:
        return IGNORED, "unknown_repository", None, None
    if not repository.is_reviewable:
        return IGNORED, "repository_disabled", repository, None

    info = pull_request_from_payload(payload["pull_request"])
    pull_request = upsert_pull_request(repository, info)

    if action not in REVIEW_ACTIONS and action != "synchronize":
        return PROCESSED, f"pull_request_{action}", repository, None

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
    if action == "synchronize":
        return _push(repository, pull_request, info.head_sha, info.base_sha, settings.review_on_push)
    try:
        run = create_run(
            pull_request, trigger=ReviewRun.Trigger.WEBHOOK, head_sha=info.head_sha, base_sha=info.base_sha
        )
    except DuplicateRun:
        return IGNORED, "already_reviewed_commit", repository, None
    return PROCESSED, "review_queued", repository, run


def _push(repository: Repository, pull_request: Any, head_sha: str, base_sha: str, enabled: bool) -> Result:
    """New commits on an open PR (RE-10): debounce, and let the newest push win."""
    if not enabled:
        return IGNORED, "push_reviews_disabled", repository, None
    superseded = supersede_queued_push_runs(pull_request)
    try:
        run = create_run(
            pull_request,
            trigger=ReviewRun.Trigger.PUSH,
            head_sha=head_sha,
            base_sha=base_sha,
            countdown=django_settings.REVIEWBOT_PUSH_DEBOUNCE_SECONDS,
        )
    except DuplicateRun:
        return IGNORED, "already_reviewed_commit", repository, None
    reason = "push_review_queued" + (f" (superseded {superseded})" if superseded else "")
    return PROCESSED, reason, repository, run


def _find_repository(connection: GitProviderConnection, payload: dict[str, Any]) -> Repository | None:
    installation_id = int((payload.get("installation") or {}).get("id") or 0)
    repo_id = int((payload.get("repository") or {}).get("id") or 0)
    return (
        Repository.objects.select_related("installation", "settings", "settings__credential")
        .filter(
            installation__connection=connection,
            installation__external_id=installation_id,
            external_id=repo_id,
        )
        .first()
    )


# --- PR comment commands (RE-17) -----------------------------------------------------------------

COMMAND_PREFIX = "/reviewbot"
# Commenters GitHub reports as owners, org members, or collaborators have repository access.
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
COMMANDS = {"review", "ignore", "resume"}


def parse_command(body: str) -> str | None:
    """Returns the command from a comment whose first line is `/reviewbot <command>`."""
    first = (body or "").strip().splitlines()[0].strip() if (body or "").strip() else ""
    parts = first.split()
    if not parts or parts[0].lower() != COMMAND_PREFIX:
        return None
    return parts[1].lower() if len(parts) > 1 else "help"


def _issue_comment(connection: GitProviderConnection, payload: dict[str, Any]) -> Result:
    if payload.get("action") != "created":
        return IGNORED, f"unsupported_action:{payload.get('action')}", None, None
    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}
    if not issue.get("pull_request"):
        return IGNORED, "not_a_pull_request", None, None
    command = parse_command(comment.get("body", ""))
    if command is None:
        return IGNORED, "no_command", None, None
    if (comment.get("user") or {}).get("type") == "Bot":
        return IGNORED, "comment_from_bot", None, None
    repository = _find_repository(connection, payload)
    if repository is None:
        return IGNORED, "unknown_repository", None, None
    if not repository.is_reviewable:
        return IGNORED, "repository_disabled", repository, None
    if comment.get("author_association") not in TRUSTED_ASSOCIATIONS:
        logger.info(
            "webhook.command_rejected",
            repo=repository.full_name,
            association=comment.get("author_association"),
        )
        return IGNORED, "insufficient_permission", repository, None
    if command not in COMMANDS:
        return IGNORED, f"unknown_command:{command[:32]}", repository, None

    pull_request, _ = PullRequest.objects.get_or_create(
        repository=repository,
        number=int(issue["number"]),
        defaults={
            "title": str(issue.get("title", ""))[:500],
            "author_login": (issue.get("user") or {}).get("login", ""),
            "html_url": issue.get("html_url", ""),
            "head_sha": "",
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
