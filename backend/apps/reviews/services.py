from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.logging import get_logger
from apps.git_providers.base import PullRequestInfo
from apps.repositories.models import Repository
from apps.repositories.services import ensure_settings
from apps.reviews.models import PullRequest, ReviewRun

logger = get_logger(__name__)


class ActiveRunExists(Exception):
    pass


class DuplicateRun(Exception):
    pass


def upsert_pull_request(repository: Repository, info: PullRequestInfo) -> PullRequest:
    defaults = {
        "title": info.title,
        "author_login": info.author_login,
        "state": info.state,
        "is_draft": info.is_draft,
        "base_ref": info.base_ref,
        "head_ref": info.head_ref,
        "head_sha": info.head_sha,
        "html_url": info.html_url,
        "updated_at": timezone.now(),
    }
    if not info.author_login:
        del defaults["author_login"]  # GitLab update events do not name the author; keep what we know
    pr, _ = PullRequest.objects.update_or_create(repository=repository, number=info.number, defaults=defaults)
    return pr


def enqueue(run_id: int, countdown: float = 0) -> None:
    from apps.reviews.tasks import run_review

    run_review.apply_async((run_id,), countdown=countdown)


def create_run(
    pull_request: PullRequest,
    *,
    trigger: str,
    head_sha: str,
    base_sha: str = "",
    created_by: Any = None,
    countdown: float = 0,
) -> ReviewRun:
    """Creates a queued run (snapshotting repo settings) and enqueues it after commit.

    Webhook- and push-triggered runs are idempotent per (pull request, head SHA, trigger); manual
    runs are not. ``countdown`` delays the job (used to debounce bursts of pushes).
    """
    repo_settings = ensure_settings(pull_request.repository)
    idempotency_key = (
        f"pr:{pull_request.pk}:{head_sha}:{trigger}"
        if trigger in (ReviewRun.Trigger.WEBHOOK, ReviewRun.Trigger.PUSH)
        else None
    )
    run = ReviewRun(
        pull_request=pull_request,
        trigger=trigger,
        head_sha=head_sha,
        base_sha=base_sha,
        idempotency_key=idempotency_key,
        credential=repo_settings.credential,
        model=repo_settings.effective_model,
        settings_snapshot=repo_settings.snapshot(),
        created_by=created_by,
    )
    try:
        with transaction.atomic():
            run.save()
    except IntegrityError as exc:
        raise DuplicateRun(idempotency_key) from exc
    transaction.on_commit(lambda: enqueue(run.pk, countdown=countdown))
    logger.info("review.queued", review_run_id=run.pk, pull_request_id=pull_request.pk, trigger=trigger)
    return run


def supersede_queued_push_runs(pull_request: PullRequest) -> int:
    """Cancels push-triggered runs that have not started; a newer push replaces them."""
    return pull_request.review_runs.filter(
        trigger=ReviewRun.Trigger.PUSH, status=ReviewRun.Status.QUEUED
    ).update(
        status=ReviewRun.Status.CANCELLED,
        status_reason="superseded",
        finished_at=timezone.now(),
    )


def request_review(pull_request: PullRequest, *, trigger: str, user: Any = None) -> ReviewRun:
    """Queues an on-demand review (dashboard or PR comment). Refuses while one is queued or running."""
    with transaction.atomic():
        locked = PullRequest.objects.select_for_update().get(pk=pull_request.pk)
        if locked.review_runs.filter(status__in=ReviewRun.ACTIVE).exists():
            raise ActiveRunExists()
        return create_run(locked, trigger=trigger, head_sha=locked.head_sha, created_by=user)


def request_manual_review(pull_request: PullRequest, user: Any) -> ReviewRun:
    return request_review(pull_request, trigger=ReviewRun.Trigger.MANUAL, user=user)
