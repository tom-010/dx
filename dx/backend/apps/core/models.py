"""Shared model bases. Feature models live in their own apps.

Primary keys are UUIDv7 (`uuid.uuid7`, Python 3.14): time-ordered like an auto-increment id
(index locality, sortable by creation), globally unique, and generated on the client side, so
offline-created rows never collide (NOTES.md §6). The column is Postgres' native `uuid`; raw
SQL can use PG 18's `uuidv7()` for the same thing.
"""

import uuid
from typing import TYPE_CHECKING, Self, TypeVar

from django.conf import settings
from django.db import models
from pydantic import BaseModel as PydanticModel

if TYPE_CHECKING:
    from apps.accounts.models import User


class BaseModel(models.Model):
    """Abstract base for every model: UUIDv7 pk, timestamps, ninja/pydantic payload helpers."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-created", "-id"]

    def set_payload(self, payload: PydanticModel) -> None:
        """Overwrite fields from a full payload (PUT): unset schema fields fall back to defaults.

        Values are passed through as they are on the schema (nested pydantic models stay
        instances, which typed JSON fields expect) — not `model_dump()`ed.
        """
        for name in type(payload).model_fields:
            setattr(self, name, getattr(payload, name))

    def set_payload_partial(self, payload: PydanticModel) -> None:
        """Apply only the fields the client actually sent (PATCH)."""
        for name in payload.model_fields_set:
            setattr(self, name, getattr(payload, name))

    def __str__(self) -> str:
        for attr in ("name", "title"):
            value = getattr(self, attr, None)
            if value:
                return str(value)
        return f"{type(self).__name__}({self.pk})"


_OwnedT = TypeVar("_OwnedT", bound="OwnedModel")


class OwnedQuerySet(models.QuerySet[_OwnedT]):
    """Queryset of an `OwnedModel`. `for_user(user)` is the one place that decides what a user
    may see; services never query owned models without it."""

    def for_user(self, user: User) -> Self:
        return self.filter(owner=user)


class OwnedModel(BaseModel):
    """Abstract base for everything that belongs to a user.

    Rule: services take the acting `User` and go through `Model.objects.for_user(user)` for
    reads, updates and deletes, so another user's rows are indistinguishable from missing ones
    (404, never 403). `apps/core/tests/test_ownership.py` enforces this for every owned resource
    registered there.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="%(class)ss"
    )

    objects = OwnedQuerySet.as_manager()

    class Meta(BaseModel.Meta):
        abstract = True
