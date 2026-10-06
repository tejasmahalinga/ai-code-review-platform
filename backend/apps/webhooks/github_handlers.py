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
from apps.reviews.models import ReviewRun
from apps.reviews.services import (
    DuplicateRun,
    create_run,
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
    installation_id = int((payload.get("installation") or {}).get("id") or 0)
    repo_id = int((payload.get("repository") or {}).get("id") or 0)
    repository = (
        Repository.objects.select_related("installation", "settings", "settings__credential")
        .filter(
            installation__connection=connection,
            installation__external_id=installation_id,
            external_id=repo_id,
        )
        .first()
    )
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
