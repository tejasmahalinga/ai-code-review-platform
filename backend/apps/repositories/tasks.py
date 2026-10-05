from __future__ import annotations

from celery import shared_task

from apps.core.logging import get_logger
from apps.git_providers.base import GitProviderError
from apps.repositories.services import github_connection, sync_github

logger = get_logger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_github_task(self) -> dict[str, int] | None:
    connection = github_connection()
    if connection is None:
        return None
    try:
        return sync_github(connection)
    except GitProviderError as exc:
        logger.warning("github.sync_failed", error=str(exc))
        raise self.retry(exc=exc) from exc
