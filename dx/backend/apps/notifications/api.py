"""Notifications: schemas, logic and the ninja router in one module.

Read, and mark read — nothing else. A notification is created by the app that had something to
say (`services.notify`), so there is no POST here; and it is retired when its source is, so
there is no DELETE either.

    GET  /api/notifications                 the inbox, paginated, `?unread=true` to narrow it
    GET  /api/notifications/unread-count    what the bell shows
    POST /api/notifications/read            the ones the inbox has just shown, read

**Reading is what the inbox does, and only the inbox.** Opening a notification navigates
somewhere, and a navigation must not quietly change the record — so nothing else in the app
marks anything read. The page marks the notifications it is showing, by id, which is also why
this takes a list rather than being "read everything": what is on page two has not been seen.

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
from ninja import Field, Router, Schema
from ninja.errors import HttpError
from ninja.pagination import PageNumberPagination, paginate

from apps.accounts.api import current_user
from apps.accounts.models import User
from apps.core.schemas import SourceRef, StrictSchema
from apps.notifications import services
from apps.notifications.models import Notification, NotificationId

router = Router(tags=["notifications"])

#: A cap, not a policy: one request marks at most a page of an inbox
#: (`NINJA_PAGINATION_PER_PAGE` is 50, its maximum 500).
MAX_READ_IDS = 500


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


class ReadIn(StrictSchema):
    """The notifications the reader has seen (`POST /api/notifications/read`)."""

    ids: list[uuid.UUID] = Field(max_length=MAX_READ_IDS)


class ReadOut(Schema):
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


@router.post("/notifications/read", response=ReadOut)
def read_notifications(request: HttpRequest, payload: ReadIn) -> ReadOut:
    """Mark the named notifications read — the ones the inbox has just shown.

    Idempotent: ones already read keep their first timestamp and are not counted. Ids that are
    not the caller's do not match their queryset, so they are ignored rather than a 404.
    """
    return ReadOut(read=services.mark_read(current_user(request), payload.ids))
