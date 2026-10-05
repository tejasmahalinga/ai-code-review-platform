from __future__ import annotations

import os

from celery import Celery
from celery.signals import setup_logging

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("reviewbot")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@setup_logging.connect
def _use_django_logging(**_kwargs: object) -> None:
    """Keep the structlog configuration from settings instead of Celery's default handlers."""
