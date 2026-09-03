"""The interface other apps program against — the notifications half of what
`apps/timeline/contracts.py` does, and deliberately the same shape.

A feature app that has something to tell the user declares a `NotificationType`, registers it,
and calls `services.notify(...)` where the thing happens. The dependency direction is always
**module → notifications**: this app knows nothing about datasets, and never imports one.

    # apps/datasets/notification_types.py
    @registry.register
    class DatasetCreated(NotificationType[Dataset]):
        key = "datasets.created"
        model = "datasets.Dataset"
        label = "Dataset created"

        def describe(self, dataset: Dataset) -> NotificationData:
            return NotificationData(title=f"New dataset: {dataset.name}", ...)

Registration happens by import: every app with notifications has a `notification_types.py`, and
`NotificationsConfig.ready()` imports all of them (`apps.py`). Nothing to add to settings.

There is no `backfill()` here, unlike an `EventType`: a notification belongs to a moment, so
there is nothing to reconcile it against and nothing to rebuild it from
(`apps/notifications/models.py`).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from apps.core.models import OwnedModel

#: "<app_label>.<snake_name>" — the app label must match the model's, so a key says where to
#: look for the code behind it. Checked at registration and again by `checks.py`.
KEY_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


class UnknownNotificationType(LookupError):
    """No type is registered under this key (a typo, or an app that is not installed)."""


class InvalidNotificationType(ValueError):
    """A type declares something the registry cannot accept — a malformed key, or a key
    another type already claims. Keys are this registry's global namespace."""


class NotificationData(BaseModel):
    """What `describe()` returns: the message, and nothing about the row it becomes.

    A module states what it wants to say; who it is for, whether it has been read and how it
    is stored are `services.notify`'s business.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=200)
    #: Optional, on purpose — plenty of notifications are a headline and nothing more.
    description: str = ""


class NotificationType[ModelT: OwnedModel](ABC):
    """One kind of message, declared by the app that owns the data it is about."""

    #: "<app_label>.<snake_name>", unique across the project.
    key: ClassVar[str]
    #: The source model as a label string ("datasets.Dataset") rather than the class, so this
    #: module never imports a feature app.
    model: ClassVar[str]
    #: Shown wherever notifications are grouped or filtered by kind.
    label: ClassVar[str]
    description: ClassVar[str] = ""

    @abstractmethod
    def describe(self, obj: ModelT) -> NotificationData:
        """The message to show about this object, as it stands now."""

    def source_label(self) -> str:
        """`"datasets.dataset"` — how the source is spelled in a row and in the API."""
        return self.model.lower()


class NotificationTypeRegistry:
    """Every registered `NotificationType`, by key. One instance, `registry`, below."""

    def __init__(self) -> None:
        self._types: dict[str, NotificationType[Any]] = {}

    def register[T: NotificationType[Any]](self, cls: type[T]) -> type[T]:
        """Class decorator. Instantiates the type once — they are stateless descriptions."""
        key = getattr(cls, "key", "")
        if not KEY_PATTERN.match(key):
            raise InvalidNotificationType(
                f"{cls.__name__}.key must be '<app_label>.<snake_name>', got {key!r}"
            )
        if key in self._types:
            other = type(self._types[key]).__name__
            raise InvalidNotificationType(f"{cls.__name__} and {other} both claim the key {key!r}")
        self._types[key] = cls()
        return cls

    def get(self, key: str) -> NotificationType[Any]:
        try:
            return self._types[key]
        except KeyError:
            raise UnknownNotificationType(f"No notification type registered as {key!r}") from None

    def for_model(self, model: type[OwnedModel]) -> list[NotificationType[Any]]:
        label = model._meta.label.lower()
        return [t for t in self._types.values() if t.source_label() == label]

    def all(self) -> list[NotificationType[Any]]:
        return sorted(self._types.values(), key=lambda t: t.key)


registry = NotificationTypeRegistry()
