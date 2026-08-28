"""Sample Celery tasks — thin wrappers around `services.py` (no logic here, only plumbing).

Trigger them from the frontend (`/tasks`) or the API (`POST /api/tasks/...`), follow them with
`GET /api/tasks/{id}/events` (SSE) or poll `GET /api/tasks/{id}`. Pattern for real tasks: put the
work in a service function, wrap it here, add an endpoint (and, if useful, a management command)
that enqueues it.
"""

# Celery inspects task signatures at runtime; keep `Task[P, R]` (celery-types) a string.
from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from celery import Task, shared_task
from celery.result import AsyncResult
from django.conf import settings
from django.core import signing
from kombu.exceptions import OperationalError

from apps.core import backups, services
from config.celery import WithRetry

# Custom state reported by long-running tasks via `update_state`.
PROGRESS = "PROGRESS"

TaskState = Literal[
    "PENDING",
    "RECEIVED",
    "STARTED",
    "PROGRESS",
    "RETRY",
    "SUCCESS",
    "FAILURE",
    "REVOKED",
    "REJECTED",
    "IGNORED",
]


@shared_task
def add(a: int, b: int) -> int:
    return services.add(a, b)


@shared_task(bind=True)
def count(self: Task[Any, int], n: int = 10, delay: float = 0.5) -> int:
    def report(current: int, total: int) -> None:
        # In eager mode there is no worker/result store to report to; the caller gets the
        # final result anyway.
        if not self.request.is_eager:
            self.update_state(state=PROGRESS, meta={"current": current, "total": total})

    return services.count_to(n, delay, report)


@shared_task(bind=True, base=WithRetry)
def dataset_summary(self: Task[Any, dict[str, int]]) -> dict[str, int]:
    """Touches the database — shows that tasks use the ORM like any other code."""
    return services.dataset_summary()


@shared_task
def fail() -> None:
    raise services.DemoFailure("This task fails on purpose")


@shared_task(bind=True, base=WithRetry)
def backup_database(self: Task[Any, str]) -> str:
    """Nightly dump to the backup storage (CELERY_BEAT_SCHEDULE); keeps BACKUP_KEEP dumps."""
    backup = backups.create_backup()
    backups.prune_backups(settings.BACKUP_KEEP)
    return backup.name


# --- Status lookup -----------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskStatus:
    id: str
    state: TaskState
    ready: bool
    result: Any = None
    error: str | None = None
    progress: tuple[int, int] | None = None


class ResultBackendUnavailable(Exception):
    pass


def status_of(result: AsyncResult[Any]) -> TaskStatus:
    """Map Celery's result object to the API's view of a task."""
    try:
        state: TaskState = result.state
        info = result.info
    except OperationalError as exc:
        raise ResultBackendUnavailable from exc
    status = TaskStatus(id=result.id, state=state, ready=result.ready())
    if state == "SUCCESS":
        return TaskStatus(**{**status.__dict__, "result": info})
    if state in ("FAILURE", "RETRY"):
        return TaskStatus(**{**status.__dict__, "error": f"{type(info).__name__}: {info}"})
    if state == PROGRESS and isinstance(info, dict):
        return TaskStatus(
            **{**status.__dict__, "progress": (int(info["current"]), int(info["total"]))}
        )
    return status


def status_by_id(task_id: str) -> TaskStatus:
    """Unknown ids look PENDING: Celery cannot distinguish "not yet picked up" from "never
    existed"; the result store also forgets results after `result_expires` (1 day)."""
    return status_of(AsyncResult(task_id))


# --- Live status stream ------------------------------------------------------------------------

# Celery's result store has no push channel (Redis pub/sub would need the worker to publish), so
# the stream polls the store; the client sees an update as soon as the worker wrote it.
WATCH_INTERVAL = 0.5  # seconds between lookups
WATCH_HEARTBEAT = 15.0  # re-send the unchanged status this often (keeps proxies from timing out)
WATCH_TIMEOUT = 10 * 60  # close the stream after this; the client reconnects (see the frontend)


def watch(
    task_id: str,
    *,
    interval: float = WATCH_INTERVAL,
    heartbeat: float = WATCH_HEARTBEAT,
    timeout: float = WATCH_TIMEOUT,
) -> Iterator[TaskStatus]:
    """Yield the task's status now and then whenever it changes, until it is ready.

    Also yields the unchanged status every `heartbeat` seconds and stops after `timeout` seconds
    even if the task is still running (unknown ids stay PENDING forever, see `status_by_id`).
    """
    deadline = time.monotonic() + timeout
    last: TaskStatus | None = None
    last_sent_at = 0.0
    while True:
        status = status_by_id(task_id)
        now = time.monotonic()
        if status != last or now - last_sent_at >= heartbeat:
            yield status
            last, last_sent_at = status, now
        if status.ready or now >= deadline:
            return
        time.sleep(interval)


# The stream is opened by the browser's EventSource, which cannot send the bearer header; the
# URL carries a signature instead (same approach as document downloads). It only grants reading
# the status of one task id, for as long as the result store keeps it.
STREAM_LINK_MAX_AGE = 24 * 60 * 60  # seconds (= CELERY result_expires)
_STREAM_SALT = "core.task-stream"


def sign_stream(task_id: str) -> str:
    return signing.dumps(task_id, salt=_STREAM_SALT)


def verify_stream(task_id: str, signature: str) -> bool:
    try:
        signed_id = signing.loads(signature, salt=_STREAM_SALT, max_age=STREAM_LINK_MAX_AGE)
    except signing.BadSignature:
        return False
    return bool(signed_id == task_id)
