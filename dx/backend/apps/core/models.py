"""Shared model bases. Feature models live in their own apps.

Primary keys are UUIDv7: time-ordered like an auto-increment id (index locality, sortable by
creation), globally unique, and cheap to generate anywhere, so offline-created rows never
collide (NOTES.md §6). The column is Postgres' native `uuid` and the default is PG 18's own
`uuidv7()`, so a raw INSERT gets a well-formed id too.

Every row also carries a `version` counter and a `deleted_at` timestamp, and every write is
mirrored into an append-only event table by a trigger (`apps/core/history.py`). Nothing is ever
hard-deleted: `soft_delete()` is an UPDATE, and the database refuses a real DELETE.

`BaseModel` is the tenant base and what a feature model extends (tenant == user, CLAUDE.md
"Multitenancy"): the `owner` column is what both isolation layers key on — the ORM scope applied
by `OwnedManager` and the row-level security policy `apps/core/rls.py` generates for every owned
table. `VersionedModel` underneath it is everything except that column, for the handful of
shared tables that predate any tenant.
"""

import uuid
from collections.abc import Container, Iterable
from typing import TYPE_CHECKING, Any, Self, TypeVar, overload

import pgtrigger
from django.conf import settings
from django.db import models
from django.db.models import Func
from django.db.models.base import ModelBase
from django.db.models.functions import Now
from django.utils import timezone
from django_scopes import ScopeError, get_scope
from pydantic import BaseModel as PydanticModel

from apps.core import lineage
from apps.core.db import NoTenantContext, current_user_id
from apps.core.history import Version, versions

# Django only imports `models.py` when it populates the app registry, so the lineage model
# has to be pulled in from here. `apps.core.lineage` imports nothing from this module at
# runtime (only under TYPE_CHECKING), so this is not a cycle — keep it that way.
from apps.core.lineage import Lineage as Lineage

# Same reason, the other way round: `apps.core.usage` imports the bases from *this* module, so
# it can only be pulled in at the end — and it has to be pulled in, or its table is not part of
# the app registry (`manage.py` records every invocation into it).

if TYPE_CHECKING:
    from apps.accounts.models import User

_ModelT = TypeVar("_ModelT", bound="VersionedModel")

# The column both isolation layers key on. Named here rather than in apps/core/rls.py so
# the system checks can use it without importing the database layer at startup.
OWNER_COLUMN = "owner_id"

# Fields that name a row for a human, in order of preference. Used by `__str__` and by
# anything that has to label a row it knows nothing else about — including an *event* row,
# which is not a `VersionedModel` and so cannot borrow its `__str__` (apps/core/revisions.py).
LABEL_FIELDS = ("name", "title")


class ActiveQuerySet(models.QuerySet[_ModelT]):
    """Queryset of a soft-deletable model; `deleted()` is the escape hatch for tooling."""

    def alive(self) -> Self:
        return self.filter(deleted_at__isnull=True)

    def deleted(self) -> Self:
        return self.filter(deleted_at__isnull=False)


class ActiveManager(models.Manager[_ModelT]):
    """Default manager of every `VersionedModel`: soft-deleted rows are simply not there.

    Django's own `_base_manager` (forward FK traversal, `refresh_from_db`, deletion collection)
    is deliberately left as the plain unfiltered manager Django creates when `Meta` names none:
    `document.owner` must keep working after the row it points at was soft-deleted, in code
    paths nobody wrote — the admin included.
    """

    def get_queryset(self) -> ActiveQuerySet[_ModelT]:
        return ActiveQuerySet(model=self.model, using=self._db).alive()


class VersionedModel(models.Model):
    """Abstract base under every model: UUIDv7 pk, timestamps, versioning, soft delete.

    **Do not extend this — extend `BaseModel` below.** Almost every model holds one user's data,
    and `BaseModel` is this class plus the `owner` column both isolation layers key on; inheriting
    here instead silently opts a table out of tenant isolation, which is why the system check
    `tenant.E001` rejects it in a feature app unless the label is listed in `SHARED_MODELS`.

    This one is right only for a table that cannot belong to a single user: `accounts.ApiToken`
    and `RefreshToken` are read while authenticating, before any tenant context exists.

    Four of these columns are owned by the database and must never be assigned in Python:

    - `id` — `uuidv7()` as a column default rather than `default=uuid.uuid7`, so a raw INSERT or
      a data migration gets a well-formed id too. Setting both would be worse than useless:
      Django prefers `default` and silently ignores `db_default`.
    - `created` — `db_default=Now()` for the same reason (`auto_now_add` is Python-side).
    - `modified` and `version` — set by the `bump_version` trigger on every UPDATE. `auto_now`
      only fires on `save()`, so a `.update()` or raw write would leave `modified` stale and the
      event row would record a timestamp that never happened.

    `version` is the authoritative ordering of the version chain. UUIDv7 ids do sort
    chronologically, but "which version came first" is a semantic claim the lineage graph makes,
    and it should not rest on clock behaviour that is only correct by construction.

    Deletes are soft (`soft_delete()`); hard deletes raise in the database. Tooling that really
    must remove rows — tenant erasure, restoring a backup, test teardown — says so explicitly
    with `apps.core.history.hard_delete()`.
    """

    id = models.UUIDField(primary_key=True, db_default=Func(function="uuidv7"), editable=False)
    created = models.DateTimeField(db_default=Now(), editable=False)
    modified = models.DateTimeField(db_default=Now(), editable=False)
    version = models.PositiveIntegerField(db_default=1, editable=False)
    deleted_at = models.DateTimeField(null=True, default=None, db_index=True, editable=False)

    # No manager here on purpose: django-stubs resolves a manager's model type per concrete
    # class, and declaring the pair on both this base *and* `BaseModel` collapses it to Any.
    # Every concrete subclass gets `objects` (soft-deleted rows hidden) plus `all_objects`
    # (everything) from `BaseModel` below or declares them itself — `test_history.py` checks
    # that none is missing.

    class Meta:
        abstract = True
        ordering = ["-created", "-id"]
        # Inherited through `class Meta(VersionedModel.Meta)`. A concrete model that declares a bare
        # `class Meta:` silently loses both triggers — `test_history.py` fails on that.
        triggers = [
            pgtrigger.Protect(name="no_hard_delete", operation=pgtrigger.Delete),
            pgtrigger.Trigger(
                name="bump_version",
                when=pgtrigger.Before,
                operation=pgtrigger.Update,
                # BEFORE, so pghistory's AFTER trigger snapshots the incremented version.
                # STATEMENT_TIMESTAMP(), not NOW(): Django renders `Now()` (the db_default of
                # `created`) as STATEMENT_TIMESTAMP on Postgres, while NOW() is the *transaction*
                # start — mixing the two makes `modified` predate `created` in the same
                # transaction, and puts two updates of one transaction at the same instant.
                func=(
                    "NEW.version = OLD.version + 1;"
                    " NEW.modified = STATEMENT_TIMESTAMP();"
                    " RETURN NEW;"
                ),
            ),
        ]

    def save(
        self,
        *,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        inserting = self._state.adding
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )
        if not inserting:
            # The `bump_version` trigger has just moved `version` and `modified` on, so the row
            # is ahead of this instance — and this instance is what the API serialises back.
            # (An INSERT needs no such read: Django fetches database defaults with RETURNING.)
            self.refresh_from_db(fields=["version", "modified"])

    def soft_delete(self) -> None:
        """Mark the row deleted. This is an UPDATE, so it bumps `version` and writes a version
        row like any other change: "was deleted" is a state the object had, and lineage edges
        pointing at earlier versions stay resolvable forever."""
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])

    def history(self) -> list[Version[Self]]:
        """Every past state of this row, oldest first — `[0]` is how it was created, `[-1]` how
        it stands now.

            for version in document.history():
                version.version, version.at, version.deleted

            document.history()[0].to_object()   # a Document, typed, as it was created

        Each entry wraps one event row (`apps/core/history.py`) and `to_object()` rebuilds this
        model's own type from it. Reads the event table, so it is a query per call — hold on to
        the list rather than indexing it twice. Raises `NotTracked` on a model that is exempt
        from versioning (`HISTORY_EXEMPT`).
        """
        return versions(type(self), self.pk)

    @overload
    def sources(self) -> list[Version[VersionedModel]]: ...

    @overload
    def sources[SourceT: VersionedModel](self, model: type[SourceT]) -> list[Version[SourceT]]: ...

    def sources(self, model: type[VersionedModel] | None = None) -> list[Version[Any]]:
        """The versions this row was built from — its lineage sources (`apps/core/lineage.py`).

            dataset.sources()                       # every source, whatever model it is
            dataset.sources(Document)[0]            # ...typed, when you know what to expect
            dataset.sources(Document)[0].to_object().name   # as the document read when imported
            dataset.sources(Document)[0].is_current()       # ...and whether it has moved since

        An edge can point at any model, so the unfiltered call can only promise
        `Version[VersionedModel]`; naming the model you expect filters to it *and* types it, which
        is what saves the caller a cast.

        Spans every version of this row, not just the current one: an edge is recorded against
        the version that consumed the source, so a later edit here must not make the row's
        origins disappear. `history()[n].sources()` is the per-version question.
        """
        found = lineage.source_versions(lineage.all_sources_of(self))
        return found if model is None else [v for v in found if issubclass(v.model, model)]

    @overload
    def derived(self) -> list[Version[VersionedModel]]: ...

    @overload
    def derived[TargetT: VersionedModel](self, model: type[TargetT]) -> list[Version[TargetT]]: ...

    def derived(self, model: type[VersionedModel] | None = None) -> list[Version[Any]]:
        """The versions of other rows that were built from this one — lineage the other way.

            document.derived(Dataset)   # the datasets imported from it, as they were then

        Every version of every derived row, oldest edge first; `model` filters and types the
        result exactly as in `sources()`. What `lineage.stale_derivations()` would have to
        rebuild is the subset whose source version is no longer current.
        """
        found = lineage.target_versions(lineage.derived_from(self).order_by("created", "id"))
        return found if model is None else [v for v in found if issubclass(v.model, model)]

    def set_payload(self, payload: PydanticModel, *, exclude: Container[str] = frozenset()) -> None:
        """Overwrite fields from a full payload (PUT): unset schema fields fall back to defaults.

        Values are passed through as they are on the schema (nested pydantic models stay
        instances, which typed JSON fields expect) — not `model_dump()`ed.

        `exclude` names schema fields that are not columns of this row — a tag list living in an
        owned through model, say. Assigning one would hit a related-manager descriptor; the
        router writes those itself (`apps/datasets/api.py::set_dataset_tags`).
        """
        for name in type(payload).model_fields:
            if name not in exclude:
                setattr(self, name, getattr(payload, name))

    def set_payload_partial(
        self, payload: PydanticModel, *, exclude: Container[str] = frozenset()
    ) -> None:
        """Apply only the fields the client actually sent (PATCH); see `set_payload`."""
        for name in payload.model_fields_set:
            if name not in exclude:
                setattr(self, name, getattr(payload, name))

    def __str__(self) -> str:
        for attr in LABEL_FIELDS:
            value = getattr(self, attr, None)
            if value:
                return str(value)
        return f"{type(self).__name__}({self.pk})"


_OwnedT = TypeVar("_OwnedT", bound="BaseModel")


class OwnedQuerySet(ActiveQuerySet[_OwnedT]):
    """Queryset of a `BaseModel`. `for_user(user)` is the explicit half of the isolation:
    callers name the user they act for; the manager below adds the ambient scope on top."""

    def for_user(self, user: User) -> Self:
        return self.filter(owner=user)


def active_scope_user() -> uuid.UUID | None:
    """The user id of the active ORM scope; None while scopes are disabled.

    Raises `ScopeError` when no scope is active at all — an owned model is being queried outside
    a request/task context, which is a bug, not an empty result.
    """
    current = get_scope()
    if not current.get("_enabled", True):
        return None
    if "user" not in current:
        raise ScopeError(
            "No tenant scope is active for this query. Owned models can only be read inside a "
            "request (TenantMiddleware), a tenant_task, `tenant_context(user_id)` or "
            "`scopes_disabled()` (admin tooling only)."
        )
    user_id = current["user"]
    if not isinstance(user_id, uuid.UUID):
        raise ScopeError(f"scope(user=...) takes the user's primary key (UUID), got {user_id!r}")
    return user_id


class OwnedManager(ActiveManager[_OwnedT]):
    """Default manager of every owned model: every queryset is filtered to the scope's user
    (`scope(user=<pk>)`, set by the middleware / `tenant_context`) and raises `ScopeError`
    without one, and soft-deleted rows are left out. `scopes_disabled()` lifts the scope — for
    tooling that runs with cross-tenant database credentials, never in request code;
    `Model.all_objects` keeps the scope but includes deleted rows (revision pages, restore).

    Django's own `_base_manager` (forward FK access, deletion cascades, `refresh_from_db`) is
    neither scope-aware nor soft-delete-aware; the RLS policy is what covers those paths, and
    an FK to a soft-deleted row must keep resolving.
    """

    #: Include soft-deleted rows. Set on the `all_objects` manager, never on `objects`.
    include_deleted = False

    def get_queryset(self) -> OwnedQuerySet[_OwnedT]:
        queryset: OwnedQuerySet[_OwnedT] = OwnedQuerySet(model=self.model, using=self._db)
        if not self.include_deleted:
            queryset = queryset.alive()
        user_id = active_scope_user()
        return queryset if user_id is None else queryset.filter(owner_id=user_id)

    def for_user(self, user: User) -> OwnedQuerySet[_OwnedT]:
        return self.get_queryset().for_user(user)


class AllOwnedManager(OwnedManager[_OwnedT]):
    """`Model.all_objects`: tenant-scoped like `objects`, but soft-deleted rows included."""

    include_deleted = True


def owned_upload_path(instance: models.Model, filename: str) -> str:
    """`upload_to` for files of owned models: `<app_label>/<owner id>/<year>/<month>/<name>`.

    Per-user prefixes keep one tenant's objects extractable (or erasable) with a prefix listing
    and make a key say whose it is. Not an access control: `/media/…` links are signed.
    """
    if not isinstance(instance, BaseModel):
        raise TypeError("owned_upload_path is for BaseModel files only")
    owner_id = instance.__dict__.get("owner_id") or current_user_id.get()
    if owner_id is None:
        raise NoTenantContext(f"{type(instance).__name__} has no owner to build a file path from")
    return f"{instance._meta.app_label}/{owner_id}/{timezone.now():%Y/%m}/{filename}"


class BaseModel(VersionedModel):
    """Abstract base for feature models: a `VersionedModel` that belongs to a user.

    Two enforcement layers ride on this class:
      1. `OwnedManager` — queries outside `scope(user=...)` raise, inside they are filtered.
      2. `manage.py rls_sync` — a Postgres row-level security policy on this model's table:
         rows whose `owner_id` differs from the request's user are invisible and unwritable,
         whatever the application code does.

    Rules: functions take the acting `User` and go through `Model.objects.for_user(user)`, so
    another user's rows are indistinguishable from missing ones (404, never 403);
    `apps/core/tests/test_ownership.py` and `test_tenancy.py` enforce this for every owned
    model. `owner` is filled in from the tenant context when a service does not set it. Never
    use a plain `ManyToManyField` between owned models — the auto-created through table has no
    `owner` column (system check tenant.E002); declare a `through=` model that is owned.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,  # erasing a user erases their data; never SET_NULL
        related_name="%(class)ss",
        editable=False,  # assigned from the tenant context, never by a form or client
    )

    objects = OwnedManager()
    all_objects = AllOwnedManager()

    class Meta(VersionedModel.Meta):
        abstract = True
        # Lists are always "this owner's rows, newest first" (VersionedModel.ordering). The name is
        # left to Django: `Index.max_name_length` is 30, so a literal `%(app_label)s_%(class)s`
        # pattern would make every app with a longer name fail `makemigrations` (models.E034).
        indexes = [models.Index(fields=["owner", "-created", "-id"])]

    def save(
        self,
        *,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        # Django stores an unset FK as None in the instance dict; the annotated attribute type
        # is non-optional, hence the dict lookup.
        if self.__dict__.get("owner_id") is None:
            user_id = current_user_id.get()
            if user_id is None:
                raise NoTenantContext(
                    f"No tenant context active; cannot assign owner for {type(self).__name__}. "
                    "Pass owner=user or wrap the call in tenant_context(user_id)."
                )
            self.owner_id = user_id
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )


# Imported for its side effect: the model has to be in the app registry, and `usage` imports the
# bases above, so this can only happen at the end of the module (see the note near the top).
from apps.core.usage import CommandRun as CommandRun  # noqa: E402
