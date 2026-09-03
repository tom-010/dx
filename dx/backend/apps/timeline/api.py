"""Timeline: schemas, logic and the ninja router in one module.

Read-only. The feed is a projection — the way to change it is to change the thing it projects,
in the app that owns it. Three operations:

    GET /api/timeline                 the feed, paginated and filtered
    GET /api/timeline/event-types     the registry, for the filter UI
    GET /api/timeline/{event_id}      one event, for a deep link

The backend ships no route, no icon and no click behaviour: an event carries a `source`
(`{type, id}`) and the SPA's own registry decides what opening it means
(`frontend/src/features/timeline/registry.ts`). That is the whole reason `source` is a pair of
plain strings and not a rendered URL.

Reads go through `TimelineEvent.objects.for_user(user)`, so another user's event does not exist
from here: 404, never 403.
"""

import uuid
from datetime import datetime
from typing import Any

from django.db.models import QuerySet
from django.http import HttpRequest
from ninja import Router, Schema
from ninja.errors import HttpError
from ninja.pagination import PageNumberPagination, paginate

from apps.accounts.api import current_user
from apps.accounts.models import User
from apps.core.schemas import SourceRef
from apps.timeline.contracts import registry
from apps.timeline.models import (
    DatePrecision,
    EventKind,
    EventStatus,
    TimelineEvent,
    TimelineEventId,
)

router = Router(tags=["timeline"])


# --- Schemas -------------------------------------------------------------------------------------


class TimelineEventOut(Schema):
    id: uuid.UUID
    event_type: str
    kind: EventKind
    status: EventStatus
    #: When it happened. `date_precision` says how much of it is meant — a `year` event is not
    #: a claim about 1 January (`apps/timeline/services.py::normalize_occurred_at`).
    occurred_at: datetime
    occurred_until: datetime | None
    date_precision: DatePrecision
    #: When the timeline learnt about it, which is a different question.
    recorded_at: datetime
    title: str
    description: str
    image_url: str
    source: SourceRef
    #: Type-specific extras; the shape is the event type's `payload_schema`.
    payload: dict[str, Any]

    @staticmethod
    def resolve_recorded_at(obj: TimelineEvent) -> datetime:
        return obj.created

    @staticmethod
    def resolve_source(obj: TimelineEvent) -> SourceRef:
        return SourceRef(type=obj.source_model, id=str(obj.source_id))


class EventTypeOut(Schema):
    """One registered event type — everything the filter UI needs to name it."""

    key: str
    kind: EventKind
    label: str
    description: str


# --- Logic ---------------------------------------------------------------------------------------


def csv_values(raw: str | None) -> list[str]:
    """A comma-separated query parameter as a list; empty entries dropped."""
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def feed(
    user: User,
    *,
    kind: EventKind | None = None,
    types: str | None = None,
    status: str = EventStatus.ACTIVE,
    since: datetime | None = None,
    until: datetime | None = None,
) -> QuerySet[TimelineEvent]:
    """The user's feed, newest first, narrowed by whatever the client asked for.

    `status` defaults to active alone: a machine-proposed event nobody has confirmed is not
    part of the record until a client says it wants to see those too.
    """
    events = TimelineEvent.objects.for_user(user)
    if kind is not None:
        events = events.filter(kind=kind)
    selected = csv_values(types)
    if selected:
        events = events.filter(event_type__in=selected)
    statuses = csv_values(status)
    if statuses:
        events = events.filter(status__in=statuses)
    if since is not None:
        events = events.filter(occurred_at__gte=since)
    if until is not None:
        events = events.filter(occurred_at__lte=until)
    return events


def get_timeline_event_for(user: User, event_id: TimelineEventId) -> TimelineEvent:
    """One event, or a 404 — another user's event does not exist from here."""
    try:
        return TimelineEvent.objects.for_user(user).get(pk=event_id)
    except TimelineEvent.DoesNotExist:
        raise HttpError(404, "Timeline event not found") from None


# --- Endpoints ----------------------------------------------------------------------------------


@router.get("/timeline", response=list[TimelineEventOut])
@paginate(PageNumberPagination)
def list_timeline_events(
    request: HttpRequest,
    kind: EventKind | None = None,
    types: str | None = None,
    status: str = EventStatus.ACTIVE,
    since: datetime | None = None,
    until: datetime | None = None,
) -> QuerySet[TimelineEvent]:
    """The feed. `types` and `status` are comma-separated lists; `since`/`until` bound
    `occurred_at`, which is what the client's date navigator sends."""
    return feed(
        current_user(request), kind=kind, types=types, status=status, since=since, until=until
    )


# Declared before `/timeline/{event_id}`: ninja matches routes in order, and "event-types"
# would otherwise be tried as a UUID.
@router.get("/timeline/event-types", response=list[EventTypeOut])
def list_timeline_event_types(request: HttpRequest) -> list[EventTypeOut]:
    """Every event type this deployment can produce — the filter UI's vocabulary."""
    return [
        EventTypeOut(key=t.key, kind=EventKind(t.kind), label=t.label, description=t.description)
        for t in registry.all()
    ]


@router.get("/timeline/{event_id}", response=TimelineEventOut)
def get_timeline_event(request: HttpRequest, event_id: uuid.UUID) -> TimelineEvent:
    """One event, so a `?event=<id>` deep link opens before the feed has loaded."""
    return get_timeline_event_for(current_user(request), TimelineEventId(event_id))
