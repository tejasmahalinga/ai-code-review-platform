from __future__ import annotations

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.core.logging import get_logger
from apps.reviews import pipeline
from apps.reviews.models import ReviewRun

logger = get_logger(__name__)

MAX_RETRIES = 5


@shared_task(bind=True, acks_late=True, max_retries=MAX_RETRIES)
def run_review(self, run_id: int) -> str | None:
    try:
        run = pipeline.execute(run_id)
    except pipeline.RetryLater as exc:
        if self.request.retries >= MAX_RETRIES:
            ReviewRun.objects.filter(pk=run_id).update(
                status=ReviewRun.Status.FAILED,
                status_reason="retries_exhausted",
                error=f"Gave up after {MAX_RETRIES} retries: {exc}",
                finished_at=timezone.now(),
            )
            run = ReviewRun.objects.select_related("pull_request__repository").get(pk=run_id)
            pipeline._notify(run, logger)
            return ReviewRun.Status.FAILED
        logger.info("review.retry_scheduled", review_run_id=run_id, countdown=exc.countdown)
        raise self.retry(countdown=min(exc.countdown, 3600), exc=exc) from exc
    return run.status if run else None


@shared_task
def reap_stuck_runs() -> dict[str, int]:
    """Fails runs stuck in RUNNING (worker died) and re-enqueues runs stuck in QUEUED (lost message)."""
    now = timezone.now()
    stuck = ReviewRun.objects.filter(
        status=ReviewRun.Status.RUNNING, started_at__lt=now - timedelta(minutes=settings.STUCK_RUN_MINUTES)
    ).update(
        status=ReviewRun.Status.FAILED,
        status_reason="timeout",
        stage=ReviewRun.Stage.TIMEOUT,
        error="The review did not finish in time (worker crash or timeout). Re-run it from the dashboard.",
        finished_at=now,
    )
    requeued = 0
    for run_id in ReviewRun.objects.filter(
        status=ReviewRun.Status.QUEUED, created_at__lt=now - timedelta(minutes=10)
    ).values_list("pk", flat=True)[:100]:
        run_review.delay(run_id)
        requeued += 1
    if stuck or requeued:
        logger.warning("review.reaper", failed=stuck, requeued=requeued)
    return {"failed": stuck, "requeued": requeued}
