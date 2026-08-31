"""Celery tasks, plus `tenant_task` — the one sanctioned way to define a task that touches
owned data.

A worker has no request and no token, so it is where a tenant leak would actually happen.
`tenant_task` takes the owner's id as the first argument and runs the function inside the
tenant context (`apps/core/db.py`): the database variable for row-level security and the ORM
scope. `apps/core/tests/test_tenancy.py` refuses `@shared_task` in tenant apps.

Sample tasks: trigger them from the frontend (`/tasks`) or the API (`POST /api/tasks/...`),
follow them with `GET /api/tasks/{id}/events` (SSE) or poll `GET /api/tasks/{id}`. Pattern for
real tasks: write the task here, add an endpoint in `apps/core/api.py` (and, if useful, a
management command) that enqueues it.
"""

# Celery inspects task signatures at runtime; keep `Task[P, R]` (celery-types) a string.
from __future__ import annotations

import functools
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Concatenate, Literal, ParamSpec, TypeVar

from celery import Task, current_task, shared_task
from celery.result import AsyncResult
from django.conf import settings
from django.core import signing
from django_scopes import scope
from kombu.exceptions import OperationalError

from apps.accounts.models import User
from apps.datasets.models import Dataset
from apps.core import backups
from apps.core.db import pin_session_tenant, tenant_context, unpin_session_tenant
from apps.core.history import history_context
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

_P = ParamSpec("_P")
_R = TypeVar("_R")


def tenant_task(
    *task_args: object, **task_kwargs: object
) -> Callable[[Callable[Concatenate[uuid.UUID, _P], _R]], Task[Any, _R]]:
    """`@tenant_task(...)` — `@shared_task(...)` for functions that read or write owned data.

    The wrapped function takes the owner's user id as its first positional argument (a UUID;
    Celery's JSON serializer may hand it over as a string). Pass ids, never model instances.

    Worker: a pinned session-level context (a long task should not hold one transaction open),
    cleared again when the task returns, so a reused connection can never carry a stale tenant.
    Eager mode (tests, CELERY_EAGER): the transaction-scoped context, because the task runs
    inline in the caller's connection.

    Both also open a pghistory context naming the task, so the version rows a background job
    writes say where they came from. With most of the work in this app happening off-request,
    that is the common path, not the edge case (apps/core/history.py).
    """

    def decorator(fn: Callable[Concatenate[uuid.UUID, _P], _R]) -> Task[Any, _R]:
        @functools.wraps(fn)
        def inner(owner_id: uuid.UUID | str, *args: _P.args, **kwargs: _P.kwargs) -> _R:
            owner = uuid.UUID(str(owner_id))
            if current_task.request.is_eager:
                return run_as_tenant_eagerly(fn, owner, *args, **kwargs)
            return run_as_tenant_in_worker(fn, owner, *args, **kwargs)

        # celery-types' overloads want literal keyword arguments; this is a pass-through.
        make_task: Callable[..., Any] = shared_task
        task: Task[Any, _R] = make_task(*task_args, **task_kwargs)(inner)
        return task

    return decorator


def run_as_tenant_eagerly[**P, R](
    fn: Callable[Concatenate[uuid.UUID, P], R], owner: uuid.UUID, *args: P.args, **kwargs: P.kwargs
) -> R:
    """Eager mode: the task runs inline on the caller's connection — transaction-scoped context."""
    with tenant_context(owner), history_context("task", task=fn.__name__):
        return fn(owner, *args, **kwargs)


def run_as_tenant_in_worker[**P, R](
    fn: Callable[Concatenate[uuid.UUID, P], R], owner: uuid.UUID, *args: P.args, **kwargs: P.kwargs
) -> R:
    """Worker: session-level context instead of one long transaction — pinned, so a reconnect
    mid-task restores it rather than silently losing the tenant, and cleared afterwards so the
    next task on this worker cannot inherit it."""
    pin_session_tenant(owner)
    try:
        with scope(user=owner), history_context("task", task=fn.__name__):
            return fn(owner, *args, **kwargs)
    finally:
        unpin_session_tenant()


# --- Sample tasks ------------------------------------------------------------------------------


class DemoFailure(Exception):
    """Raised on purpose by the `fail` sample task."""


@shared_task
def add(a: int, b: int) -> int:
    return a + b


@shared_task(bind=True)
def count(self: Task[Any, int], n: int = 10, delay: float = 0.5) -> int:
    """Slow loop that reports progress after each step (demo for long-running work)."""
    for current in range(1, n + 1):
        time.sleep(delay)
        # In eager mode there is no worker/result store to report to; the caller gets the
        # final result anyway.
        if not self.request.is_eager:
            self.update_state(state=PROGRESS, meta={"current": current, "total": n})
    return n


@tenant_task(base=WithRetry)
def dataset_summary(owner_id: uuid.UUID) -> dict[str, int]:
    """Touches owned data — shows the `tenant_task` pattern: the owner's id comes in and the
    context is set up around the function, so the ORM scope and the policy are both in place."""
    datasets = Dataset.objects.for_user(User.objects.get(pk=owner_id))
    return {
        "datasets": datasets.count(),
        "rows": sum(datasets.values_list("row_count", flat=True)),
    }


@shared_task
def fail() -> None:
    raise DemoFailure("This task fails on purpose")


@shared_task(bind=True, base=WithRetry)
def backup_database(self: Task[Any, str]) -> str:
    """Nightly dump to the backup storage (CELERY_BEAT_SCHEDULE); keeps BACKUP_KEEP dumps.

    Routed to the `maintenance` queue (CELERY_TASK_ROUTES): it needs cross-tenant database
    credentials, which only the maintenance worker has (`./scripts/celery.sh maintenance`).
    """
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
