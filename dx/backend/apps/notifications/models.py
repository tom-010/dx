"""Notifications: one row per thing that happened and is worth telling one user about.

The same shape as `apps/timeline`, deliberately — a materialized table, and a registry the
feature apps declare into — but answering a different question. The timeline is *the record*:
one row per (source, type) describing the source as it stands now, rebuildable from the rows it
projects. A notification is *the message*: it is addressed to a person, it is unread until they
look at it, and there is nothing to reconstruct it from once it is gone.

    create a dataset ──▶ datasets writes the Dataset
                    └──▶ notifications.notify("datasets.created", dataset)

Where it therefore differs from the timeline, and why:

- **`read_at`.** The one piece of state a notification has of its own. `title` and `description`
  come from the source and are refreshed by `notify()`; `read_at` is the reader's, and is never
  reset by a later `notify()` of the same thing.
- **No `backfill()`, no `rebuild`.** "Which rows should have a notification right now" is not a
  question with an answer: a notification belongs to a moment, not to a state. A projection you
  cannot rebuild is exactly what this is, and pretending otherwise would invite a command that
  re-notifies everyone about everything they already read.
- **No `kind`, `status` or date precision.** A notification happened when it was recorded, and
  `created` says so.

Shared with the timeline (and for the same reasons — the full argument is in
`apps/timeline/models.py`): the tenant is the user, so `OwnedModel.owner` is who the
notification is *for*; the source is `source_model` + `source_id`, a label and a UUID rather
than a foreign key, which is also the `{type, id}` the SPA's own registry keys its routes on;
and `notify()` is an upsert on (source, type), so the same thing never queues up twice.
"""

from __future__ import annotations

import uuid
from typing import NewType

from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.history import tracked
from apps.core.models import OwnedModel

NotificationId = NewType("NotificationId", uuid.UUID)


@tracked
class Notification(OwnedModel):
    """One message for one user. Written only through `apps/notifications/services.py`."""

    #: The registered type, "<app_label>.<snake_name>" (`contracts.NotificationType.key`).
    notification_type = models.CharField(max_length=100, db_index=True)

    title = models.CharField(max_length=200)
    #: Optional — a notification is allowed to be a headline and nothing more.
    description = models.TextField(blank=True, default="")

    #: What it is about: `"datasets.dataset"` (a lower-cased model label) and that row's id.
    #: Deliberately not a foreign key — see the module docstring.
    source_model = models.CharField(max_length=100)
    source_id = models.UUIDField()

    #: When the reader looked at it; NULL while unread. The only column the reader owns.
    read_at = models.DateTimeField(null=True, blank=True, default=None)

    # No custom manager: "unread" is one `filter(read_at__isnull=True)`, spelled out at the
    # two places that need it (`services.unread_for`, the list endpoint) rather than hidden
    # behind a queryset subclass that would have to be threaded through both managers.

    class Meta(OwnedModel.Meta):
        constraints = [
            # One notification per (source row, type), so re-running the thing that produced it
            # refreshes the message instead of stacking another copy. Conditioned on
            # `deleted_at`, like every unique constraint here.
            models.UniqueConstraint(
                fields=["source_model", "source_id", "notification_type"],
                condition=Q(deleted_at__isnull=True),
                name="notification_uniq_source_type",
            )
        ]
        indexes = [
            # Replaces the owner/-created index of `OwnedModel` with the same thing plus the
            # unread flag: the bell's count and the page's list are the only two reads.
            models.Index(
                fields=["owner", "read_at", "-created", "-id"], name="notification_inbox_idx"
            ),
            models.Index(fields=["source_model", "source_id"], name="notification_source_idx"),
        ]

    @staticmethod
    def example() -> Notification:
        return Notification(
            notification_type="datasets.created",
            title="New dataset: Expenses 2026",
            description="Imported from expenses.csv",
            source_model="datasets.dataset",
            source_id=uuid.uuid7(),
        )

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    def mark_read(self) -> None:
        """Idempotent: reading something twice is not a second event."""
        if self.read_at is not None:
            return
        self.read_at = timezone.now()
        # `sources=[]`: reading a message is not deriving it from anything, and the step — if
        # the caller is inside one — is the enclosing block's.
        self.save(operation=None, sources=[], update_fields=["read_at"])

    def __str__(self) -> str:
        return self.title
