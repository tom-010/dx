"""Core router: infrastructure endpoints that are not tied to a feature."""

import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Literal

from django.http import HttpRequest, StreamingHttpResponse
from ninja import Field, Router, Schema, Status
from ninja.errors import HttpError

from apps.accounts.api import current_user
from apps.core import health, lineage, revisions, tasks

router = Router(tags=["core"])


class HealthOut(Schema):
    status: Literal["ok"]


class CheckOut(Schema):
    name: str
    ok: bool
    detail: str


class ReadyOut(Schema):
    status: Literal["ok", "fail"]
    checks: list[CheckOut]


@router.get("/health", response=HealthOut, auth=None)
def health_check(request: HttpRequest) -> HealthOut:
    """Liveness: the process answers requests. No dependencies are touched (a database outage
    must not get the app restarted). Public."""
    return HealthOut(status="ok")


@router.get("/ready", response={200: ReadyOut, 503: ReadyOut}, auth=None)
def ready(request: HttpRequest) -> Status[ReadyOut]:
    """Readiness: database reachable and migrated, Celery broker up, storage buckets present.
    503 with the failing checks otherwise — gate load balancers / compose on this. Public."""
    checks = health.readiness()
    ok = all(check.ok for check in checks)
    body = ReadyOut(
        status="ok" if ok else "fail",
        checks=[CheckOut(name=c.name, ok=c.ok, detail=c.detail) for c in checks],
    )
    return Status(200 if ok else 503, body)


# --- Sample Celery tasks (apps/core/tasks.py) ---------------------------------------------------

tasks_router = Router(tags=["tasks"])


class TaskProgress(Schema):
    current: int
    total: int


class TaskOut(Schema):
    """Snapshot of a Celery task. Until `ready` is true, follow it via `stream_url`
    (Server-Sent Events) or poll `GET /api/tasks/{id}`."""

    id: str
    state: tasks.TaskState
    ready: bool
    result: object | None = Field(default=None, description="Return value once SUCCESS")
    error: str | None = None
    progress: TaskProgress | None = None
    stream_url: str = Field(
        description="Signed SSE endpoint: one `status` event (this schema) per state change"
    )


class AddIn(Schema):
    a: int
    b: int


class CountIn(Schema):
    n: int = Field(default=10, ge=1, le=600)
    delay: float = Field(default=0.5, ge=0, le=10, description="Seconds per step")


def _task_out(status: tasks.TaskStatus) -> TaskOut:
    progress = None
    if status.progress is not None:
        progress = TaskProgress(current=status.progress[0], total=status.progress[1])
    return TaskOut(
        id=status.id,
        state=status.state,
        ready=status.ready,
        result=status.result,
        error=status.error,
        progress=progress,
        stream_url=f"/api/tasks/{status.id}/events?sig={tasks.sign_stream(status.id)}",
    )


@tasks_router.post("/tasks/add", response=TaskOut)
def run_add(request: HttpRequest, payload: AddIn) -> TaskOut:
    """Instant task: a + b."""
    return _task_out(tasks.status_of(tasks.add.delay(payload.a, payload.b)))


@tasks_router.post("/tasks/count", response=TaskOut)
def run_count(request: HttpRequest, payload: CountIn) -> TaskOut:
    """Slow task that reports PROGRESS (n steps of `delay` seconds)."""
    return _task_out(tasks.status_of(tasks.count.delay(payload.n, payload.delay)))


@tasks_router.post("/tasks/dataset-summary", response=TaskOut)
def run_dataset_summary(request: HttpRequest) -> TaskOut:
    """Task that reads the database (the caller's dataset and row counts) — a `tenant_task`:
    the worker runs it inside the caller's tenant context."""
    owner_id = current_user(request).pk
    return _task_out(tasks.status_of(tasks.dataset_summary.delay(owner_id)))


@tasks_router.post("/tasks/fail", response=TaskOut)
def run_fail(request: HttpRequest) -> TaskOut:
    """Task that raises, to see FAILURE handling end to end."""
    return _task_out(tasks.status_of(tasks.fail.delay()))


@tasks_router.get("/tasks/{task_id}", response=TaskOut)
def get_task(request: HttpRequest, task_id: str) -> TaskOut:
    try:
        return _task_out(tasks.status_by_id(task_id))
    except tasks.ResultBackendUnavailable:
        raise HttpError(503, "Task result store (Redis) is not reachable") from None


def _sse(statuses: Iterator[tasks.TaskStatus]) -> Iterator[bytes]:
    """Encode each status as an SSE `status` event whose data is a `TaskOut` JSON document."""
    for status in statuses:
        yield f"event: status\ndata: {_task_out(status).model_dump_json()}\n\n".encode()


@tasks_router.get("/tasks/{task_id}/events", auth=None)
def stream_task_events(request: HttpRequest, task_id: str, sig: str) -> StreamingHttpResponse:
    """Server-Sent Events with the task's status: one `status` event now and one per change,
    then the connection closes once the task is ready (text/event-stream, not part of the JSON
    contract — use `TaskOut.stream_url`, which carries the signature).

    The stream also closes after `tasks.WATCH_TIMEOUT`; EventSource reconnects on its own.
    """
    if not tasks.verify_stream(task_id, sig):
        raise HttpError(403, "Invalid or expired stream link")
    try:
        tasks.status_by_id(task_id)  # fail with a JSON error now, not inside the stream
    except tasks.ResultBackendUnavailable:
        raise HttpError(503, "Task result store (Redis) is not reachable") from None
    response = StreamingHttpResponse(_sse(tasks.watch(task_id)), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # nginx: do not buffer the stream
    return response


# --- Revision history (apps/core/revisions.py) ---------------------------------------------------

history_router = Router(tags=["history"])


class ChangeOut(Schema):
    field: str
    old: str | None
    new: str | None


class SourceOut(Schema):
    """A lineage edge: the exact source *version* this revision was built from."""

    model: str
    label: str
    object_id: uuid.UUID
    version: int
    pgh_id: uuid.UUID
    is_stale: bool = Field(description="The source has been changed since this was derived")


class RevisionOut(Schema):
    pgh_id: uuid.UUID
    object_id: uuid.UUID
    model: str
    version: int
    label: str
    at: datetime
    schema_tag: str
    schema_known: bool = Field(
        description="False when the row predates the recorded field sets: its values cannot be "
        "diffed reliably (apps/core/history.py, SCHEMA_TAG)"
    )
    deleted: bool
    changes: list[ChangeOut]
    unknown_fields: list[str] = Field(
        description="Fields not tracked at both ends of the comparison — not a change"
    )
    archived: dict[str, str] = Field(description="Values of fields dropped since (pgh_archive)")
    context_id: uuid.UUID | None
    sources: list[SourceOut]
    is_related: bool = Field(
        description="A child row written in the same save (an explicit m2m through model, say) "
        "rather than a version of the object itself"
    )
    description: str = Field(description="What a child row points at, in words")


class RevisionGroupOut(Schema):
    """One save: everything a single request or task wrote, however many tables it touched."""

    context_id: uuid.UUID | None
    source: str
    at: datetime
    revisions: list[RevisionOut]


class HistoryOut(Schema):
    model: str
    object_id: uuid.UUID
    current_version: int
    groups: list[RevisionGroupOut]


@history_router.get("/history/{resource}/{object_id}", response=HistoryOut)
def get_history(request: HttpRequest, resource: str, object_id: uuid.UUID) -> HistoryOut:
    """Every version of one object, newest first, grouped into the saves that produced them.

    `resource` is the model name in lower case (`dataset`, `document`, `mediaitem`); the tracked
    models are the ones with history (`apps/core/history.py`). Another tenant's object is a 404,
    like everywhere else — the event tables carry the same row-level security policy.
    """
    user = current_user(request)
    model = revisions.resource_model(resource)
    if model is None:
        raise HttpError(404, f"No versioned resource named {resource!r}")
    obj = model.all_objects.for_user(user).filter(pk=object_id).first()
    if obj is None:
        raise HttpError(404, f"{model.__name__} {object_id} not found")

    groups = revisions.group_by_context(revisions.revisions_of(obj))
    return HistoryOut(
        model=model.__name__,
        object_id=obj.pk,
        current_version=obj.version,
        groups=[RevisionGroupOut.from_orm(group) for group in groups],
    )


# --- Lineage graph (apps/core/lineage.py) --------------------------------------------------------


class NodeOut(Schema):
    """One object in the graph — the live row, not a version of it."""

    object_id: uuid.UUID
    model: str
    label: str
    version: int
    deleted: bool
    depth: int = Field(
        description="Steps from the object asked about: negative upstream (what it came from), "
        "positive downstream (what came from it)"
    )


class EdgeOut(Schema):
    source_id: uuid.UUID
    target_id: uuid.UUID
    source_version: int
    is_stale: bool = Field(description="The source has changed since this edge was recorded")
    created: datetime


class GraphOut(Schema):
    root_id: uuid.UUID
    nodes: list[NodeOut]
    edges: list[EdgeOut]


@history_router.get("/lineage/{resource}/{object_id}", response=GraphOut)
def get_lineage(
    request: HttpRequest, resource: str, object_id: uuid.UUID, depth: int = 3
) -> GraphOut:
    """What this object was built from and what was built from it, transitively.

    `resource` is the model name in lower case, as for the revision history. Edges point at a
    *version* of a source, so an edge marked `is_stale` says the source has moved on since —
    the derived object was built from something that no longer reads that way.
    """
    user = current_user(request)
    model = revisions.resource_model(resource)
    if model is None:
        raise HttpError(404, f"No versioned resource named {resource!r}")
    obj = model.all_objects.for_user(user).filter(pk=object_id).first()
    if obj is None:
        raise HttpError(404, f"{model.__name__} {object_id} not found")
    return GraphOut.from_orm(lineage.graph(obj, depth=depth))
