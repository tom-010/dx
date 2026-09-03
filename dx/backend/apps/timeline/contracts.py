"""The interface other apps program against. Small, typed, and the only thing they import.

A feature app that has something to show declares an `EventType`, registers it, and calls
`services.record(...)` where the change happens. The dependency direction is always
**module → timeline**: the timeline knows nothing about documents, and never imports one.

    # apps/documents/timeline_events.py
    @registry.register
    class DocumentUploaded(EventType[Document]):
        key = "documents.uploaded"
        kind = EventKind.TECHNICAL
        model = "documents.Document"
        label = "Document uploaded"
        payload_schema = DocumentUploadedPayload

        def describe(self, document: Document) -> EventData:
            return EventData(occurred_at=document.created, title=document.title, ...)

One `describe()` rather than five getters: the timeline validates the whole description at once
(it is a pydantic model), and the module's side of the contract stays one function. `backfill()`
answers the other question — "which rows should have this event right now" — which is what
makes the table rebuildable (`services.rebuild`, `manage.py rebuild_timeline`).

Registration happens by import: every app with events has a `timeline_events.py`, and
`TimelineConfig.ready()` imports all of them (`apps.py`). Nothing to add to settings.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, ClassVar

from django.apps import apps as django_apps
from django.db.models import QuerySet
from pydantic import BaseModel, ConfigDict, Field

from apps.core.models import OwnedModel
from apps.timeline.models import DatePrecision, EventKind, EventStatus

#: "<app_label>.<snake_name>" — the app label must match the model's, so a key says where to
#: look for the code behind it. Checked at registration and again by `checks.py`.
KEY_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


class UnknownEventType(LookupError):
    """No event type is registered under this key (a typo, or an app that is not installed)."""


class InvalidEventType(ValueError):
    """An event type declares something the registry cannot accept — a malformed key, or a key
    another type already claims. Keys are the timeline's global namespace."""


class EventData(BaseModel):
    """Everything the timeline needs to know about one event — what `describe()` returns.

    Deliberately not the model: a module states facts about its own row and never touches the
    timeline's columns, its owner or its identity. `services.record` turns this into a row.
    """

    model_config = ConfigDict(extra="forbid")

    #: When it happened (not when it was recorded). Normalised by `record()` for day/month/year
    #: precision, so callers pass whatever they have.
    occurred_at: datetime
    #: The end of a span; leave None for a point in time.
    occurred_until: datetime | None = None
    date_precision: DatePrecision = DatePrecision.DATETIME
    title: str = Field(max_length=200)
    description: str = ""
    image_url: str = Field(default="", max_length=500)
    #: Type-specific extras. A `payload_schema` on the type validates it.
    payload: BaseModel | dict[str, Any] = Field(default_factory=dict)
    #: Phase 1 writes `ACTIVE`; a machine-proposed event starts `SUGGESTED`.
    status: EventStatus = EventStatus.ACTIVE


class EventType[ModelT: OwnedModel](ABC):
    """One kind of thing that can appear in the feed, declared by the app that owns the data."""

    #: "<app_label>.<snake_name>", unique across the project.
    key: ClassVar[str]
    kind: ClassVar[EventKind]
    #: The source model as a label string ("documents.Document") rather than the class, so this
    #: module never imports a feature app.
    model: ClassVar[str]
    #: Shown in the filter UI.
    label: ClassVar[str]
    description: ClassVar[str] = ""
    #: Validated on write; a payload that does not fit is a bug in the calling app, not data.
    payload_schema: ClassVar[type[BaseModel] | None] = None

    @abstractmethod
    def describe(self, obj: ModelT) -> EventData:
        """What this object looks like on a card, right now."""

    def backfill(self) -> QuerySet[ModelT]:
        """Every object that should currently have this event — the definition `rebuild()`
        works from, in both directions: rows missing an event get one, and events whose source
        is no longer in here are retired. The default is "all live rows of the model"; override
        to narrow it (only processed documents, only published notes, …)."""
        model: type[ModelT] = django_apps.get_model(self.model)
        return model.objects.all()

    def source_label(self) -> str:
        """`"documents.document"` — how the source is spelled in a row and in the API."""
        return self.model.lower()


class EventTypeRegistry:
    """Every registered `EventType`, by key. One instance, `registry`, below."""

    def __init__(self) -> None:
        self._types: dict[str, EventType[Any]] = {}

    def register[T: EventType[Any]](self, cls: type[T]) -> type[T]:
        """Class decorator. Instantiates the type once — they are stateless descriptions."""
        key = getattr(cls, "key", "")
        if not KEY_PATTERN.match(key):
            raise InvalidEventType(
                f"{cls.__name__}.key must be '<app_label>.<snake_name>', got {key!r}"
            )
        if key in self._types:
            other = type(self._types[key]).__name__
            raise InvalidEventType(f"{cls.__name__} and {other} both claim the key {key!r}")
        self._types[key] = cls()
        return cls

    def get(self, key: str) -> EventType[Any]:
        try:
            return self._types[key]
        except KeyError:
            raise UnknownEventType(f"No timeline event type registered as {key!r}") from None

    def for_model(self, model: type[OwnedModel]) -> list[EventType[Any]]:
        """The types whose source is this model — a dict scan over a handful of entries."""
        label = model._meta.label.lower()
        return [t for t in self._types.values() if t.source_label() == label]

    def all(self) -> list[EventType[Any]]:
        return sorted(self._types.values(), key=lambda t: t.key)


registry = EventTypeRegistry()
