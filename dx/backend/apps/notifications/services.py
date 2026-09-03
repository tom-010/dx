"""The write side of notifications. Every row in `Notification` comes through here.

Three calls, and a feature app only ever needs the first two:

    notifications.notify("datasets.created", dataset)   # tell the owner about it
    notifications.remove(dataset)                       # the thing is gone; retire its messages
    notifications.unread_for(user)                      # what the bell counts

`notify` runs in the caller's transaction and has no side effect beyond the row, so the domain
change and the message land together or not at all. It is an upsert on (source, type): calling
it twice refreshes the message from a fresh `describe()` rather than queueing a second copy,
and **it never un-reads** something the user already looked at.

**Lineage: `sources=[]`, and that is a decision, not an omission.** The timeline records the
row its card was computed from, because `rebuild_timeline` really does recompute cards from
it. Nothing ever recomputes a notification — it has no `backfill()` and no rebuild — so
claiming the edge would only fill `stale_derivations()` with rows nobody will ever act on. The
message was true when it was sent; the step that sent it is named (`operation`), and what it
was built from is nothing.
"""

from __future__ import annotations

import uuid

from django.db.models import QuerySet

from apps.accounts.models import User
from apps.core.models import OwnedModel
from apps.notifications.contracts import NotificationData, registry
from apps.notifications.models import Notification

#: The step name every notification write carries into the lineage graph — the same step
#: whatever produced it ("this message was computed from that row").
OPERATION = "notify"


def _existing(source_label: str, source_id: uuid.UUID, key: str) -> Notification | None:
    """This source's notification of this type: the live one, else the most recently retired.

    Retired rows are reused rather than skipped, so a source that comes back keeps the id its
    deep links already point at — and its read state with it.
    """
    rows = Notification.all_objects.filter(
        source_model=source_label, source_id=source_id, notification_type=key
    )
    live = rows.alive().first()
    return live if live is not None else rows.deleted().order_by("-created").first()


def notify(key: str, source: OwnedModel) -> Notification:
    """Tell the source's owner about it, and return the notification.

    Runs in the caller's transaction (a request already has one). Raises
    `UnknownNotificationType` for an unregistered key.
    """
    notification_type = registry.get(key)
    data: NotificationData = notification_type.describe(source)
    label = notification_type.source_label()

    existing = _existing(label, source.pk, key)
    if existing is None:
        return Notification.create(
            operation=OPERATION,
            sources=[],  # a message, not a derivation — see the module docstring
            # The owner of the row is who hears about it. With tenant == user there is nobody
            # else it could be addressed to, and copying it off the source means a message can
            # never land in a different tenant than the thing it is about.
            owner_id=source.owner_id,
            notification_type=key,
            title=data.title,
            description=data.description,
            source_model=label,
            source_id=source.pk,
        )
    existing.title = data.title
    existing.description = data.description
    # A retired notification whose source is being announced again is live once more; there is
    # no `restore()` in this project — putting a row back is clearing `deleted_at` and saving.
    existing.deleted_at = None
    # `read_at` is deliberately untouched: refreshing the wording of something the reader has
    # already seen must not put it back in their unread list.
    existing.save(operation=OPERATION, sources=[])
    return existing


def remove(source: OwnedModel, key: str | None = None) -> int:
    """Retire a source's notifications — all of them, or one type. Returns how many.

    Soft, like every delete here. Call it where the source is soft-deleted: a message pointing
    at something the user can no longer open is a dead end.
    """
    label = type(source)._meta.label.lower()
    notifications = Notification.objects.filter(source_model=label, source_id=source.pk)
    if key is not None:
        notifications = notifications.filter(notification_type=key)
    retired = list(notifications)
    for notification in retired:
        notification.soft_delete()
    return len(retired)


def notifications_for(source: OwnedModel) -> QuerySet[Notification]:
    """Every live notification about this row, newest first."""
    return Notification.objects.filter(
        source_model=type(source)._meta.label.lower(), source_id=source.pk
    )


def unread_for(user: User) -> QuerySet[Notification]:
    """What the bell counts and the page shows first."""
    return Notification.objects.for_user(user).filter(read_at__isnull=True)


def mark_all_read(user: User) -> int:
    """Read everything at once. Returns how many were still unread.

    A loop rather than a queryset `.update()`: an `.update()` is versioned by the trigger but
    runs no `save()`, so it would leave the version rows with no caller and no request behind
    them. An inbox is small enough for this to be the cheaper trade.
    """
    pending = list(unread_for(user))
    for notification in pending:
        notification.mark_read()
    return len(pending)
