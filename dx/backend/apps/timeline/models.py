"""The timeline: one flat, rebuildable projection of everything that happened to a tenant.

A `TimelineEvent` is not a source of truth. The feature apps stay that: a document row is the
document. The timeline is a **projection** — one denormalised row per (source row, event type),
written in the same transaction as the change it describes, so the feed is one indexed query
instead of a union over every app that might have something to show.

    upload a document ──▶ documents writes the Document
                     └──▶ timeline.record("documents.uploaded", document)

Common fields (`title`, `description`, `image_url`, `occurred_at`) carry what a card needs, so
rendering the feed never resolves a source row; `payload` carries the type-specific extras,
validated on write against the event type's pydantic schema (`contracts.py`). What the source
*was* is a reference, not a foreign key: `source_model` + `source_id`.

Adapted from `backend/timeline.md`, which was written without this project's invariants in
view. What changed, and why:

- **No `patient` column.** The tenant is the user (CLAUDE.md "Multitenancy"), so `OwnedModel`'s
  `owner` is the timeline's scope, and row-level security is what keeps one feed out of
  another. `record()` copies the owner off the source row, so an event can never land in a
  different tenant than the thing it describes.
- **No `actor` column.** With tenant == user the actor is always the owner. Who wrote a row and
  through which request is already recorded, for every write in the project, on the version
  (`version.caller`, `version.request_id` — `.claude/rules/versioning.md`).
- **No `ContentType` generic foreign key.** Every primary key here is a UUID, so the whole
  reference is a model label plus that UUID — which is also exactly the `{type, id}` the API
  ships to the frontend registry. A `ContentType` FK would add a join and a second way to spell
  the same thing. There is deliberately no FK to the source: an event outlives a retired source
  row, and `Lineage` keeps its source pointers the same way.
- **Deletes are soft.** `services.remove()` retires events rather than deleting them, and the
  unique constraint is conditioned on `deleted_at__isnull=True` so a source that comes back
  gets its event back (`record()` revives the retired row rather than colliding with it).
- **No `post_delete` signal.** Nothing in this project hard-deletes except tenant erasure,
  which walks every owned table anyway and takes these rows with it. Modules call `remove()`
  where they soft-delete — one explicit line in the same transaction, which is also what
  `.claude/rules/versioning.md` says about cascade: application logic, never a signal.
"""

from __future__ import annotations

import uuid
from typing import NewType

from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from apps.core.history import tracked
from apps.core.models import OwnedModel

TimelineEventId = NewType("TimelineEventId", uuid.UUID)


class EventKind(models.TextChoices):
    """The two visual weights of the feed, decided on the backend so every client agrees.

    `TECHNICAL` is the quiet log of what was done to the data ("you uploaded a file");
    `REAL_WORLD` is what the data says happened, and is what a reader actually came for.
    """

    TECHNICAL = "technical", "Technical"
    REAL_WORLD = "real_world", "Real world"


class DatePrecision(models.TextChoices):
    """How much of `occurred_at` is meant. A year-only event must not pretend to be 1 January,
    so the client groups and formats by this rather than by the timestamp alone."""

    DATETIME = "datetime", "Date and time"
    DAY = "day", "Day"
    MONTH = "month", "Month"
    YEAR = "year", "Year"


class EventStatus(models.TextChoices):
    """`ACTIVE` is everything phase 1 writes. The other two are for events a machine proposed
    and a person has not confirmed yet (`timeline.md` §6); the feed filters on this."""

    ACTIVE = "active", "Active"
    SUGGESTED = "suggested", "Suggested"
    REJECTED = "rejected", "Rejected"


# Every write is captured in TimelineEventEvent by a database trigger (apps/core/history.py):
# a projection is rebuildable, but "when did this card change, and to what" is still a question
# with an answer, and the row is owned tenant data like any other.
@tracked
class TimelineEvent(OwnedModel):
    """One card in one user's feed. Written only through `apps/timeline/services.py`."""

    #: The registered event type, "<app_label>.<snake_name>" (`contracts.EventType.key`).
    event_type = models.CharField(max_length=100, db_index=True)
    kind = models.CharField(max_length=20, choices=EventKind.choices)
    status = models.CharField(
        max_length=20, choices=EventStatus.choices, default=EventStatus.ACTIVE
    )

    #: The sort key: when the event happened, not when it was recorded (`created` is that).
    #: Normalised to 12:00 UTC of the period start for day/month/year precision, so the date a
    #: reader sees never shifts by a timezone conversion — `services.normalize_occurred_at`.
    occurred_at = models.DateTimeField()
    #: The end of a span; NULL for a point in time.
    occurred_until = models.DateTimeField(null=True, blank=True)
    date_precision = models.CharField(
        max_length=10, choices=DatePrecision.choices, default=DatePrecision.DAY
    )

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    #: A URL the SPA can already load (an API path). The timeline neither stores nor proxies
    #: images.
    image_url = models.CharField(max_length=500, blank=True, default="")
    #: Type-specific extras, validated against `EventType.payload_schema` on write. Anything
    #: every card shows is a column instead.
    payload = models.JSONField(default=dict, blank=True)

    #: What this event is about: `"documents.document"` (a lower-cased model label) and that
    #: row's id. Deliberately not a foreign key — see the module docstring.
    source_model = models.CharField(max_length=100)
    source_id = models.UUIDField()

    class Meta(OwnedModel.Meta):
        # The feed's own order, which is not the creation order every other model has.
        ordering = ["-occurred_at", "-id"]
        constraints = [
            # One event per (source row, type). Conditioned on `deleted_at`, like every unique
            # constraint here: a retired event must not reserve its slot forever.
            models.UniqueConstraint(
                fields=["source_model", "source_id", "event_type"],
                condition=Q(deleted_at__isnull=True),
                name="timeline_uniq_source_type",
            ),
            models.CheckConstraint(
                condition=Q(occurred_until__isnull=True) | Q(occurred_until__gte=F("occurred_at")),
                name="timeline_range_valid",
            ),
        ]
        indexes = [
            # Replaces the owner/-created index of `OwnedModel`: every read of this table is
            # "this owner's feed, newest first" or "this source's events".
            models.Index(fields=["owner", "-occurred_at", "-id"], name="timeline_feed_idx"),
            models.Index(fields=["source_model", "source_id"], name="timeline_source_idx"),
        ]

    @staticmethod
    def example() -> TimelineEvent:
        return TimelineEvent(
            event_type="documents.uploaded",
            kind=EventKind.TECHNICAL,
            status=EventStatus.ACTIVE,
            occurred_at=timezone.now(),
            date_precision=DatePrecision.DATETIME,
            title="Expenses 2026.pdf",
            description="PDF, 3 pages",
            payload={"mime_type": "application/pdf", "page_count": 3},
            source_model="documents.document",
            source_id=uuid.uuid7(),
        )

    def __str__(self) -> str:
        return f"{self.event_type}: {self.title}"
