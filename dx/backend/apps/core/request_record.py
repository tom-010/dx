"""The HTTP request behind a write, kept with the versions and edges it produced.

A version says *which code* wrote it (`pgh_stack`) and *which step* (`pgh_context`). This is the
third question a reviewer asks of a row that came in through the API: *what did the client
actually send?* — the method and path, the headers, and the JSON body, as they arrived.

    request.method, request.path                   what was asked
    request.headers                                 who asked, from where, how (redacted, below)
    request.body                                    the payload — JSON only, never a file

**One row per request that wrote something.** `TenantMiddleware` puts the live request in a
contextvar for the duration of the request (`request_scope`); the first `save()` inside it
records the row (`record_current`), in the same transaction as the write, and stamps its id on
every version (`Event.pgh_request`, through the same transaction-local setting as the stack) and
every edge (`Lineage.request`) that follows. A request that writes nothing — a GET, a 404, a
validation error — leaves no row. Writes with no request around them — a task, a command, a
shell — have nothing to record, and `pgh_request` stays NULL.

**Owned, not shared.** A request body is the most tenant-identifying thing in the database, so
this table carries `owner` and gets the same row-level security policy as everything else, is
erased with the tenant and exported by `pull_tenant` — where `sent_headers`, `sent_query` and
`sent_body` are scrubbed (`apps/core/scrub.py`), because a body is PII by definition. It is the
reason none of this could live in `pghistory_context`, which every tenant can read.

**What is not kept.** Credentials: `Authorization`, `Cookie` and the CSRF token are recorded as
present but redacted — a backup with live bearer tokens in it is a bad artefact whoever owns the
row. Files: a multipart body is not readable after the view consumed the upload (Django raises),
and would not belong here if it were. Large bodies: JSON above `BODY_LIMIT` is not stored, since
truncated JSON is not JSON; the size is. `body_status` says which of these applied.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import pgtrigger
import structlog.contextvars
from django.conf import settings
from django.db import models
from django.db.models import Func
from django.db.models.functions import Now
from django.http.request import RawPostDataException

from apps.core.db import NoTenantContext, current_user_id

if TYPE_CHECKING:
    from django.http import HttpRequest

#: JSON bodies above this are recorded by size only. Truncated JSON is not JSON.
BODY_LIMIT = 64 * 1024

#: Header values recorded as `<redacted>`: present, so a reviewer sees they were sent, never
#: stored. Matched case-insensitively against the canonical names `request.headers` yields.
REDACTED_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "x-csrftoken"})
REDACTED = "<redacted>"

_current: ContextVar[HttpRequest | None] = ContextVar("current_request", default=None)
#: The id of the row recorded for the current request, once one is. Per request, like the
#: request itself; the request is one transaction, so there is no state to get out of step.
_recorded: ContextVar[uuid.UUID | None] = ContextVar("recorded_request", default=None)


class RequestRecord(models.Model):
    """One HTTP request that wrote at least one row, as it arrived. Immutable."""

    id = models.UUIDField(primary_key=True, db_default=Func(function="uuidv7"), editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+", editable=False
    )
    #: django-structlog's id for the request — every log line of it carries the same value.
    request_id = models.CharField(max_length=64, blank=True, db_index=True, editable=False)
    method = models.CharField(max_length=16, editable=False)
    path = models.TextField(editable=False)
    # `sent_*`: what the client sent. Named distinctively because `apps/core/scrub.py` allowlists
    # PII by *field name* across every model, and `body` would have caught a note's text too.
    sent_query = models.JSONField(default=dict, editable=False)
    sent_headers = models.JSONField(default=dict, editable=False)
    content_type = models.CharField(max_length=200, blank=True, editable=False)
    #: The JSON body, or None — see `body_status` for why.
    sent_body = models.JSONField(null=True, default=None, editable=False)
    body_size = models.PositiveIntegerField(default=0, editable=False)
    #: "json" (stored), "none" (no body or not JSON), "too-large", "invalid-json", "unreadable"
    #: (a multipart body the view already consumed).
    body_status = models.CharField(max_length=16, editable=False)
    created = models.DateTimeField(db_default=Now(), editable=False)

    objects = models.Manager()

    class Meta:
        ordering = ["-created"]
        indexes = [models.Index(fields=["owner", "-created"])]
        triggers = [
            pgtrigger.Protect(name="no_hard_delete", operation=pgtrigger.Delete),
            pgtrigger.Protect(name="append_only", operation=pgtrigger.Update),
        ]

    def __str__(self) -> str:
        return f"{self.method} {self.path}"

    @staticmethod
    def example() -> RequestRecord:
        """A request shaped like a real API write; `owner` comes from the tenant context."""
        return RequestRecord(
            request_id=str(uuid.uuid4()),
            method="PATCH",
            path="/api/datasets/01a0-example",
            sent_query={},
            sent_headers={"Content-Type": "application/json", "Authorization": REDACTED},
            content_type="application/json",
            sent_body={"name": "renamed"},
            body_size=19,
            body_status="json",
        )


@contextmanager
def request_scope(request: HttpRequest) -> Iterator[None]:
    """Make `request` the one the writes inside the block may be recorded against."""
    token = _current.set(request)
    recorded = _recorded.set(None)
    try:
        yield
    finally:
        _recorded.reset(recorded)
        _current.reset(token)


def current_request_id() -> uuid.UUID | None:
    """The id of the recorded row for the current request, or None — outside a request, or
    before its first write."""
    return _recorded.get()


def record_current() -> uuid.UUID | None:
    """Record the current request if there is one and it has not been recorded yet.

    Called by `lineage.declare_write_origin` on every save, inside the write's transaction: the
    first call in a request inserts the row, every later one returns the same id for free. Outside
    a request there is nothing to do and it returns None.
    """
    request = _current.get()
    if request is None:
        return None
    recorded = _recorded.get()
    if recorded is not None:
        return recorded

    owner_id = current_user_id.get()
    if owner_id is None:
        raise NoTenantContext("recording a request needs the tenant context its writes run in")
    body, size, status = _body_of(request)
    record = RequestRecord(
        owner_id=owner_id,
        request_id=str(structlog.contextvars.get_contextvars().get("request_id", "")),
        method=request.method or "",
        path=request.path,
        sent_query={k: v if len(v) > 1 else v[0] for k, v in request.GET.lists()},
        sent_headers=_redacted(request),
        content_type=request.content_type or "",
        sent_body=body,
        body_size=size,
        body_status=status,
    )
    record.save()
    _recorded.set(record.pk)
    return record.pk


def _redacted(request: HttpRequest) -> dict[str, str]:
    return {
        name: (REDACTED if name.lower() in REDACTED_HEADERS else value)
        for name, value in request.headers.items()
    }


def _body_of(request: HttpRequest) -> tuple[Any, int, str]:
    """`(body, size, status)` — the JSON body when there is one and it is small enough."""
    if not (request.content_type or "").startswith("application/json"):
        return None, 0, "none"
    try:
        raw = request.body
    except RawPostDataException:
        return None, 0, "unreadable"  # a stream the view already consumed (multipart)
    if not raw:
        return None, 0, "none"
    if len(raw) > BODY_LIMIT:
        return None, len(raw), "too-large"
    try:
        return json.loads(raw), len(raw), "json"
    except ValueError:
        return None, len(raw), "invalid-json"
