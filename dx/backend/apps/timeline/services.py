"""The write side of the timeline. Every row in `TimelineEvent` comes through here.

Four calls, and a module only ever needs the first three:

    timeline.record("documents.uploaded", document)   # created, or refreshed if it exists
    timeline.remove(document)                         # retire its events (soft delete)
    timeline.events_for(document)                     # what the feed shows about it
    timeline.rebuild()                                # the projection from scratch

`record` runs in the caller's transaction and has no side effect beyond the row, so the
domain change and the card it produces land together or not at all. It is safe to call
twice — the second call re-reads `describe()` and updates the row, which is exactly what a
rename needs:

    document.title = "Arztbrief Dr. Müller"
    document.save(operation=None, sources=[])
    timeline.record("documents.uploaded", document)   # the card now says the new name

The event is *derived from* the source row, so every write here records that as lineage
(`.claude/rules/versioning.md`): if the document changes, this row has to be recomputed, and
`stale_derivations(document)` will say so.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable
from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from apps.core.models import OwnedModel
from apps.timeline.contracts import EventData, EventType, registry
from apps.timeline.models import DatePrecision, EventKind, TimelineEvent

#: The step name every timeline write carries into the lineage graph. It is the same step
#: whatever produced it — "this card was computed from that row" — so it reads the same on
#: every node (`VersionedModel.save` on what belongs in `operation`).
OPERATION = "record timeline event"

#: Noon, not midnight: a day-, month- or year-precision event stored at 00:00 UTC becomes the
#: previous day for every reader west of Greenwich. 12:00 survives every real timezone.
PERIOD_HOUR = 12


def normalize_occurred_at(moment: dt.datetime, precision: DatePrecision) -> dt.datetime:
    """The instant a `DAY`/`MONTH`/`YEAR` event is stored at: 12:00 UTC of the period's first
    day. `DATETIME` is stored as given. Done here rather than in callers, so every module gets
    it right by not thinking about it."""
    if precision == DatePrecision.DATETIME:
        return moment
    at = moment.astimezone(dt.UTC)
    day = 1 if precision in (DatePrecision.MONTH, DatePrecision.YEAR) else at.day
    month = 1 if precision == DatePrecision.YEAR else at.month
    return at.replace(month=month, day=day, hour=PERIOD_HOUR, minute=0, second=0, microsecond=0)


def _payload_dict(
    event_type: EventType[Any], payload: BaseModel | dict[str, Any]
) -> dict[str, Any]:
    """The payload as jsonb, validated against the type's schema if it declares one.

    A payload that does not fit is a bug in the calling app — it built the description — so
    this raises rather than storing something the client cannot read.
    """
    schema = event_type.payload_schema
    if schema is None:
        return payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
    try:
        validated = payload if isinstance(payload, schema) else schema.model_validate(payload)
    except PydanticValidationError as error:
        raise ValidationError(
            f"payload of {event_type.key} does not match {schema.__name__}: {error}"
        ) from error
    return validated.model_dump(mode="json")


def _existing(source_label: str, source_id: uuid.UUID, key: str) -> TimelineEvent | None:
    """This source's event of this type: the live one, else the most recently retired one.

    Retired rows are reused rather than skipped, so a source that is restored (or re-recorded
    after a `remove()`) keeps the id its deep links already point at.
    """
    rows = TimelineEvent.all_objects.filter(
        source_model=source_label, source_id=source_id, event_type=key
    )
    live = rows.alive().first()
    return live if live is not None else rows.deleted().order_by("-created").first()


def record(key: str, source: OwnedModel) -> TimelineEvent:
    """Create or refresh this source's event of type `key`, and return it.

    Runs in the caller's transaction (open one around the domain write and this call together;
    a request already has one). Raises `UnknownEventType` for an unregistered key and
    `ValidationError` for a description the type's own schema rejects.
    """
    event_type = registry.get(key)
    data: EventData = event_type.describe(source)
    if data.occurred_until is not None and data.occurred_until < data.occurred_at:
        raise ValidationError(f"{key}: occurred_until is before occurred_at")
    fields: dict[str, Any] = {
        "kind": EventKind(event_type.kind),
        "status": data.status,
        "occurred_at": normalize_occurred_at(data.occurred_at, data.date_precision),
        "occurred_until": data.occurred_until,
        "date_precision": data.date_precision,
        "title": data.title,
        "description": data.description,
        "image_url": data.image_url,
        "payload": _payload_dict(event_type, data.payload),
    }

    event = _existing(event_type.source_label(), source.pk, key)
    if event is None:
        return TimelineEvent.create(
            operation=OPERATION,
            sources=[source],
            owner_id=source.owner_id,
            event_type=key,
            source_model=event_type.source_label(),
            source_id=source.pk,
            **fields,
        )
    for name, value in fields.items():
        setattr(event, name, value)
    # A retired event whose source is being recorded again is alive once more; there is no
    # `restore()` in this project — putting a row back is clearing `deleted_at` and saving.
    event.deleted_at = None
    event.save(operation=OPERATION, sources=[source])
    return event


def record_many(key: str, sources: Iterable[OwnedModel]) -> int:
    """`record` over many rows; returns how many were written.

    A loop, not a `bulk_create(update_conflicts=True)`: `bulk_create` skips `save()`, and a
    write that skips `save()` records no lineage and no call stack. At this project's size
    (CLAUDE.md: design for 10 users) one statement per row is the cheaper trade.
    """
    return sum(1 for source in sources if record(key, source) is not None)


def remove(source: OwnedModel, key: str | None = None) -> int:
    """Retire this source's events — all of them, or one type. Returns how many were retired.

    Soft, like every delete here: the row keeps its place in the version chain, and a later
    `record()` revives it. Call it where the source is soft-deleted or drops out of
    `backfill()`.
    """
    label = type(source)._meta.label.lower()
    events = TimelineEvent.objects.filter(source_model=label, source_id=source.pk)
    if key is not None:
        events = events.filter(event_type=key)
    retired = list(events)
    for event in retired:
        event.soft_delete()
    return len(retired)


def events_for(source: OwnedModel) -> QuerySet[TimelineEvent]:
    """Every live event about this row, newest first."""
    return TimelineEvent.objects.filter(
        source_model=type(source)._meta.label.lower(), source_id=source.pk
    )


def rebuild(*, key: str | None = None, chunk_size: int = 1000) -> dict[str, tuple[int, int]]:
    """Recompute the projection for the tenant in context: `{key: (recorded, retired)}`.

    For each type: `record` every row of `backfill()`, then retire the events of that type
    whose source is no longer in it. Idempotent — running it twice changes nothing the second
    time — which is what makes it both the migration path for data that predates an event type
    and the repair for a projection that drifted.
    """
    types = [registry.get(key)] if key is not None else registry.all()
    counts: dict[str, tuple[int, int]] = {}
    for event_type in types:
        recorded = 0
        live_ids: set[uuid.UUID] = set()
        for source in event_type.backfill().iterator(chunk_size=chunk_size):
            record(event_type.key, source)
            live_ids.add(source.pk)
            recorded += 1
        stale = TimelineEvent.objects.filter(event_type=event_type.key).exclude(
            source_id__in=live_ids
        )
        retired = 0
        for event in list(stale):
            event.soft_delete()
            retired += 1
        counts[event_type.key] = (recorded, retired)
    return counts
