"""Notifications: schemas, logic and the ninja router in one module.

Read, and mark read — nothing else. A notification is created by the app that had something to
say (`services.notify`), so there is no POST here; and it is retired when its source is, so
there is no DELETE either.

    GET  /api/notifications                 the inbox, paginated, `?unread=true` to narrow it
    GET  /api/notifications/unread-count    what the bell shows
    POST /api/notifications/{id}/read       one message, read
    POST /api/notifications/read            all of them, read

Like the timeline, the backend ships a `source` (`{type, id}`) and no route: where a
notification leads is the SPA's registry's business
(`frontend/src/features/notifications/registry.ts`).

Reads go through `Notification.objects.for_user(user)`, so another user's notification does not
exist from here: 404, never 403.
"""

import uuid
from datetime import datetime

from django.db.models import QuerySet
from django.http import HttpRequest
from ninja import Router, Schema
from ninja.errors import HttpError
from ninja.pagination import PageNumberPagination, paginate

from apps.accounts.api import current_user
from apps.accounts.models import User
from apps.core.schemas import SourceRef
from apps.notifications import services
from apps.notifications.models import Notification, NotificationId

router = Router(tags=["notifications"])


# --- Schemas -------------------------------------------------------------------------------------


class NotificationOut(Schema):
    id: uuid.UUID
    notification_type: str
    title: str
    #: Optional: "" when the message is a headline and nothing more.
    description: str
    created: datetime
    #: When the reader looked at it; null while unread.
    read_at: datetime | None
    source: SourceRef

    @staticmethod
    def resolve_source(obj: Notification) -> SourceRef:
        return SourceRef(type=obj.source_model, id=str(obj.source_id))


class UnreadCountOut(Schema):
    """The bell's indicator. Its own tiny endpoint because it is polled and the list is not."""

    unread: int


class ReadAllOut(Schema):
    read: int


# --- Logic ---------------------------------------------------------------------------------------


def inbox(user: User, *, unread: bool = False) -> QuerySet[Notification]:
    """The user's notifications, newest first; unread only when asked."""
    notifications = Notification.objects.for_user(user)
    return notifications.filter(read_at__isnull=True) if unread else notifications


def get_notification_for(user: User, notification_id: NotificationId) -> Notification:
    """One notification, or a 404 — another user's does not exist from here."""
    try:
        return Notification.objects.for_user(user).get(pk=notification_id)
    except Notification.DoesNotExist:
        raise HttpError(404, "Notification not found") from None


# --- Endpoints ----------------------------------------------------------------------------------


@router.get("/notifications", response=list[NotificationOut])
@paginate(PageNumberPagination)
def list_notifications(request: HttpRequest, unread: bool = False) -> QuerySet[Notification]:
    """The inbox. `?unread=true` is the same list narrowed to what has not been seen."""
    return inbox(current_user(request), unread=unread)


# Declared before `/notifications/{notification_id}`: ninja matches routes in order, and
# "unread-count" would otherwise be tried as a UUID.
@router.get("/notifications/unread-count", response=UnreadCountOut)
def count_unread_notifications(request: HttpRequest) -> UnreadCountOut:
    """Just the number, so the bell can ask for it often without fetching the list."""
    return UnreadCountOut(unread=services.unread_for(current_user(request)).count())


@router.post("/notifications/read", response=ReadAllOut)
def read_all_notifications(request: HttpRequest) -> ReadAllOut:
    """Mark everything read. Returns how many were still unread."""
    return ReadAllOut(read=services.mark_all_read(current_user(request)))


@router.post("/notifications/{notification_id}/read", response=NotificationOut)
def read_notification(request: HttpRequest, notification_id: uuid.UUID) -> Notification:
    """Mark one read. Idempotent — reading it twice keeps the first timestamp."""
    notification = get_notification_for(current_user(request), NotificationId(notification_id))
    notification.mark_read()
    return notification
