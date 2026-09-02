"""Shared model bases. Feature models live in their own apps.

Primary keys are UUIDv7: time-ordered like an auto-increment id (index locality, sortable by
creation), globally unique, and cheap to generate anywhere, so offline-created rows never
collide (NOTES.md §6). The column is Postgres' native `uuid` and the default is PG 18's own
`uuidv7()`, so a raw INSERT gets a well-formed id too.

Every row also carries a `version` counter and a `deleted_at` timestamp, and every write is
mirrored into an append-only event table by a trigger (`apps/core/history.py`). Nothing is ever
hard-deleted: `soft_delete()` is an UPDATE, and the database refuses a real DELETE.

`OwnedModel` is the tenant base and what a feature model extends (tenant == user, CLAUDE.md
"Multitenancy"): the `owner` column is what both isolation layers key on — the ORM scope applied
by `OwnedManager` and the row-level security policy `apps/core/rls.py` generates for every owned
table. `VersionedModel` underneath it is everything except that column, for the handful of
shared tables that predate any tenant.
"""

import uuid
from collections.abc import Container, Iterable, Sequence
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, NoReturn, Self, TypeVar, overload

import pgtrigger
from django.conf import settings
from django.db import models, transaction
from django.db.models import Func
from django.db.models.base import ModelBase
from django.db.models.functions import Now
from django.utils import timezone
from django_scopes import ScopeError, get_scope
from pydantic import BaseModel as PydanticModel

from apps.core import lineage
from apps.core.db import NoTenantContext, current_user_id
from apps.core.history import Version, event_model_for, hard_delete, history_context, versions

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

    def create(self, **fields: object) -> NoReturn:
        """Refused: this spelling cannot say where the row came from.

        Lineage is not optional here — every write states its `sources` and its `operation`, if
        only to say `None` — and a manager's `create()` cannot carry those two keywords (mypy's
        Django plugin checks every keyword against the model's fields). So it does not exist;
        `Model.create(..., operation=..., sources=...)` is the one way to insert a row.
        """
        name = self.model.__name__
        raise TypeError(
            f"{name}.objects.create() cannot record lineage. Use {name}.create(..., "
            "operation=<short name of the step, for a reviewer> | None, "
            "sources=<rows this was computed from> | [] | None). Both are required, so "
            "that not recording where a row came from is a decision and not an accident; "
            f"see {name}.save.__doc__ for what to write."
        )

    def delete(self) -> tuple[int, dict[str, int]]:
        """Soft delete every matched row. One UPDATE, and still fully versioned.

        `.update()` goes round `save()`, but the capture is a *database trigger*, so each row
        still gets its version bump and its event row (`.claude/rules/versioning.md`). Rows that
        are already deleted are skipped rather than re-stamped: deleting twice is not an event.

        Django's cascade does not run here, by design — cascade is application logic in this
        project (`apps/datasets/api.py` is the worked example), because a collector that fired
        on a soft delete would also have to be right about restores and erasure, and it is not.
        """
        count = self.alive().update(deleted_at=timezone.now())
        return count, {self.model._meta.label: count} if count else {}

    def hard_delete(self) -> tuple[int, dict[str, int]]:
        """Really remove the matched rows *and their version history*.

        The deliberate exception to "nothing is deleted": tenant erasure, credential purging and
        test teardown. Everything else means `delete()`.
        """
        with hard_delete():
            ids = list(self.values_list("pk", flat=True))
            events = event_model_for(self.model)
            if events is not None and ids:
                events._base_manager.filter(**{"pgh_obj_id__in": ids}).delete()
            return super().delete()


class ActiveManager(models.Manager[_ModelT]):
    """Default manager of every `VersionedModel`: soft-deleted rows are simply not there.

    Django's own `_base_manager` (forward FK traversal, `refresh_from_db`, deletion collection)
    is deliberately left as the plain unfiltered manager Django creates when `Meta` names none:
    `document.owner` must keep working after the row it points at was soft-deleted, in code
    paths nobody wrote — the admin included.
    """

    def get_queryset(self) -> ActiveQuerySet[_ModelT]:
        return ActiveQuerySet(model=self.model, using=self._db).alive()

    # django-stubs types `Manager.all()` and `.filter()` as returning a plain `QuerySet`, which
    # loses `alive()`, `deleted()` and `hard_delete()` the moment a caller goes through the
    # manager — `Model.objects.filter(...).hard_delete()` would not type-check even though it
    # works. Narrowed here so the queryset's own API survives the hop.
    def all(self) -> ActiveQuerySet[_ModelT]:
        return self.get_queryset()

    def filter(self, *args: models.Q, **kwargs: object) -> ActiveQuerySet[_ModelT]:
        return self.get_queryset().filter(*args, **kwargs)

    def deleted(self) -> ActiveQuerySet[_ModelT]:
        """The soft-deleted rows. Only ever non-empty on `all_objects`, which is the point:
        `objects.deleted()` asks for rows `objects` has already filtered out."""
        return self.get_queryset().deleted()


def _described(description: str | None) -> dict[str, str]:
    """The `description` metadata for a history context, or nothing — a key that is absent reads
    better in the context table than one that is None."""
    return {"description": description} if description else {}


class VersionedModel(models.Model):
    """Abstract base under every model: UUIDv7 pk, timestamps, versioning, soft delete.

    **Do not extend this — extend `OwnedModel` below.** Almost every model holds one user's data,
    and `OwnedModel` is this class plus the `owner` column both isolation layers key on; inheriting
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
    # class, and declaring the pair on both this base *and* `OwnedModel` collapses it to Any.
    # Every concrete subclass gets `objects` (soft-deleted rows hidden) plus `all_objects`
    # (everything) from `OwnedModel` below or declares them itself — `test_history.py` checks
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

    def save(  # type: ignore[override]  # deliberately stricter: two keywords Django's lacks
        self,
        *,
        operation: str | None,
        sources: Sequence[VersionedModel] | None,
        operation_description: str | None = None,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        """Write the row, and say which operation did it and what it was built from.

        `operation` and `sources` are required — `None` is an answer, an omitted keyword is a
        `TypeError`. Both are recorded in the same transaction as the row, against the version
        this save produces, so the lineage graph (`apps/core/lineage.py`) never has a node whose
        origin was forgotten.

        **`operation`** — the *name of the step* that produced this write. It is the label a data
        reviewer sees on the node in the lineage graph, so write it for them: a short verb phrase
        saying what was done, stable across runs so the same step reads the same every time.

            operation="summarise notes"            ✓ what the step does
            operation="convert to EUR"             ✓
            operation="extract entities (opus)"    ✓ the tool, when it is part of the method
            operation="merge monthly parts v3"     ✓ a version, when the method changed

            operation="api" / "update" / "save"    ✗ how the write arrived, not what it did — the
                                                      request or task context records that already
            operation="apps/notes/api.py:312"      ✗ where in the code — the edge records the
                                                      whole call stack (`Lineage.stack`) itself
            operation="Alice's Q3 report"          ✗ about the data, not the step; and this label
                                                      lands in a table every tenant can read

            operation=None            no step of its own: a person edited this through the API,
                                      or the enclosing `history_context("…")` block names it

        The rule of thumb: **code that derives data names its operation; a human typing into a
        form is `None`.** Around 80% of writes are the latter, and for them the request context
        ("api", the method) is the whole story.

        **`sources`** — the rows whose *content* this write used. The test: if that row changed,
        would this one have to be recomputed? Then it is a source. Structural links are not
        (`owner`, the dataset a tag belongs to): those are foreign keys and mean "belongs to",
        not "was computed from".

            sources=[rates, totals]   these rows, at the versions they are at right now
            sources=[]                built from nothing but this write — user input, a fixture
            sources=None              whatever the enclosing `deriving(...)` block says; nothing
                                      outside one

        **`operation_description`** (optional) — the longer form, for the same reviewer: what the
        step did *in this run*. Parameters, the model and prompt version, counts, anything that
        went differently — "14 chunks, opus, prompt v3; 2 chunks over the limit were skipped".
        Still about the operation, never about the data, for the same reason: it is stored in
        the history context beside the name (`apps/core/history.py`, "Context"). It needs an
        `operation` to describe — on its own it is a `ValueError`, not a note that quietly
        attaches to somebody else's step.

            report.save(operation="convert to EUR", sources=[rates, totals])
            report.save(operation=None, sources=[])                 # a person edited it
            with history_context("convert to EUR"), deriving(rates, totals):
                report.save(operation=None, sources=None)           # the block says it all
        """
        if operation_description is not None and operation is None:
            raise ValueError(
                "operation_description describes a named operation: pass operation=… with it. "
                "Inside a history_context() block, describe the block instead."
            )
        inserting = self._state.adding
        step = (
            history_context(operation, **_described(operation_description))
            if operation
            else nullcontext()
        )
        with transaction.atomic(using=using), step:
            super().save(
                force_insert=force_insert,
                force_update=force_update,
                using=using,
                update_fields=update_fields,
            )
            if not inserting:
                # The `bump_version` trigger has just moved `version` and `modified` on, so the
                # row is ahead of this instance — and this instance is what the API serialises
                # back. (An INSERT needs no such read: Django fetches database defaults with
                # RETURNING.)
                self.refresh_from_db(fields=["version", "modified"])
            lineage.record_sources(self, sources)

    def delete(
        self, using: str | None = None, keep_parents: bool = False
    ) -> tuple[int, dict[str, int]]:
        """Deleting is soft. `soft_delete()` is the same thing, said explicitly.

        Overridden rather than left to raise, because "delete" is what every caller already
        writes — Django's admin, a `ModelForm`, a service, a shell — and the useful behaviour is
        for all of them to do the right thing rather than to fail. The database trigger stays
        where it is: it now guards raw SQL and data migrations, the paths this override cannot
        reach.

        Returns Django's `(count, {label: count})` so it is a drop-in; nothing cascades, because
        cascade is application logic here.
        """
        self.soft_delete()
        return 1, {self._meta.label: 1}

    @classmethod
    def create(
        cls,
        *,
        operation: str | None,
        sources: Sequence[VersionedModel] | None,
        operation_description: str | None = None,
        **fields: object,
    ) -> Self:
        """Insert a row, saying what it was built from and which operation did it — one statement.

            Summary.create(text=..., operation="summarise notes", sources=[dataset],
                           operation_description="14 chunks, opus, prompt v3")
            Summary.create(text=..., operation=None, sources=[])      # a person typed it
            Summary.create(text=..., operation=None, sources=None)    # the enclosing blocks'

        What to put in `sources`, `operation` and `operation_description` — including what *not*
        to — is spelled out on `save()`, which this calls. In short: `sources` are the rows this
        one was computed from; `operation` is the short, stable name of the step, written for
        the reviewer reading the lineage graph, and `None` when a human made the write.

        This is the only way to insert a row: `Model.objects.create()` is refused
        (`ActiveQuerySet.create`), because a manager's `create()` cannot carry these keywords —
        mypy's Django plugin checks every keyword of it against the model's fields — and a write
        that cannot state its lineage is not a write this project makes.

        The price: field names here are checked by Django at runtime (`TypeError` on a typo),
        not by mypy — the plugin does not look inside a classmethod.
        """
        obj = cls(**fields)
        obj.save(
            operation=operation,
            sources=sources,
            operation_description=operation_description,
            force_insert=True,
        )
        return obj

    def soft_delete(self) -> None:
        """Mark the row deleted. This is an UPDATE, so it bumps `version` and writes a version
        row like any other change: "was deleted" is a state the object had, and lineage edges
        pointing at earlier versions stay resolvable forever."""
        self.deleted_at = timezone.now()
        # `sources=[]`: a delete inside a `deriving()` block must not claim the row was built from
        # the block's inputs. Retiring a row is not deriving it from anything. The step, if any,
        # is the enclosing block's.
        self.save(update_fields=["deleted_at"], operation=None, sources=[])

    def hard_delete(self) -> tuple[int, dict[str, int]]:
        """Really remove this row and the version rows that describe it.

        The exception, not the tool: erasing a tenant, purging a spent credential, tearing down
        a test. It lifts the `no_hard_delete` trigger for the duration, so Django's cascade —
        which *does* run here — can take the related rows with it.

        It removes **this row's** history, not the history of whatever the cascade collected;
        walking a tenant's tables is `apps/core/tenants.py`'s job and it does it explicitly.
        """
        with hard_delete():
            events = event_model_for(type(self))
            if events is not None:
                events._base_manager.filter(**{"pgh_obj_id": self.pk}).delete()
            return super().delete()

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

    @staticmethod
    def example() -> VersionedModel:
        """One filled-in instance of this model, unsaved — built by the model itself.

            with acting_as(user):
                dataset = save_example(Dataset.example())      # apps/core/examples.py

        Every concrete model overrides this; the base is here to say that they must, and the
        system check `example.E001` is what notices when one does not. Fill in every required
        field, build a required foreign key by calling *its* model's `example()`, and leave
        `owner`, `id`, `created`, `modified` and `version` alone — the tenant context and the
        database own those.
        """
        raise NotImplementedError("every model defines its own example(); apps/core/examples.py")

    def __str__(self) -> str:
        for attr in LABEL_FIELDS:
            value = getattr(self, attr, None)
            if value:
                return str(value)
        return f"{type(self).__name__}({self.pk})"


_OwnedT = TypeVar("_OwnedT", bound="OwnedModel")


class OwnedQuerySet(ActiveQuerySet[_OwnedT]):
    """Queryset of an `OwnedModel`. `for_user(user)` is the explicit half of the isolation:
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

    # Same narrowing as `ActiveManager`, one step further down: these return the *owned*
    # queryset, so `for_user()` stays reachable after a `.filter()`.
    def all(self) -> OwnedQuerySet[_OwnedT]:
        return self.get_queryset()

    def filter(self, *args: models.Q, **kwargs: object) -> OwnedQuerySet[_OwnedT]:
        return self.get_queryset().filter(*args, **kwargs)

    def deleted(self) -> OwnedQuerySet[_OwnedT]:
        return self.get_queryset().deleted()


class AllOwnedManager(OwnedManager[_OwnedT]):
    """`Model.all_objects`: tenant-scoped like `objects`, but soft-deleted rows included."""

    include_deleted = True


def owned_upload_path(instance: models.Model, filename: str) -> str:
    """`upload_to` for files of owned models: `<app_label>/<owner id>/<year>/<month>/<name>`.

    Per-user prefixes keep one tenant's objects extractable (or erasable) with a prefix listing
    and make a key say whose it is. Not an access control: `/media/…` links are signed.
    """
    if not isinstance(instance, OwnedModel):
        raise TypeError("owned_upload_path is for OwnedModel files only")
    owner_id = instance.__dict__.get("owner_id") or current_user_id.get()
    if owner_id is None:
        raise NoTenantContext(f"{type(instance).__name__} has no owner to build a file path from")
    return f"{instance._meta.app_label}/{owner_id}/{timezone.now():%Y/%m}/{filename}"


class OwnedModel(VersionedModel):
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

    def save(  # type: ignore[override]  # see VersionedModel.save
        self,
        *,
        operation: str | None,
        sources: Sequence[VersionedModel] | None,
        operation_description: str | None = None,
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
            operation=operation,
            sources=sources,
            operation_description=operation_description,
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )


# Imported for its side effect: the model has to be in the app registry, and `usage` imports the
# bases above, so this can only happen at the end of the module (see the note near the top).
from apps.core.usage import CommandRun as CommandRun  # noqa: E402
