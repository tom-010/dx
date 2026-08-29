"""Celery application. `celery -A config worker` (see ./scripts/celery.sh).

Configuration comes from Django settings with the `CELERY_` prefix (`config/settings.py`).
Dev and prod run a real worker (`./scripts/celery.sh` auto-reloads on code changes); with
`CELERY_EAGER=true` (tests) tasks run inline in the calling process and need no broker.
"""

import logging.config
import os

import celery
from celery import Celery
from celery.signals import setup_logging
from django.conf import settings
from django_structlog.celery.steps import DjangoStructLogInitStep

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("dx")
app.config_from_object("django.conf:settings", namespace="CELERY")
# Loads `tasks.py` from every app in INSTALLED_APPS (`apps.core.tasks`, ...).
app.autodiscover_tasks()
# Structured task logs with the request_id of the caller (config/logging.py).
app.steps["worker"].add(DjangoStructLogInitStep)


@setup_logging.connect
def configure_worker_logging(**_kwargs: object) -> None:
    """The worker logs like the web process: Django's LOGGING (config/logging.py) — plain
    lines in dev, JSON in prod. Connecting this signal makes Celery skip its own logging
    setup, which would otherwise replace the root handler with Celery's text format."""
    logging.config.dictConfig(settings.LOGGING)


class WithRetry(celery.Task):  # type: ignore[type-arg]  # celery-types wants Task[P, R]; base classes are generic-free
    """Base for tasks that talk to flaky things (network, other services): retries with
    exponential backoff + jitter. Use as `@shared_task(bind=True, base=WithRetry)`."""

    autoretry_for = (Exception,)
    retry_kwargs = {"max_retries": 5}
    retry_backoff = True
    retry_jitter = True
