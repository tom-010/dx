"""Version history: every write to a tracked table is mirrored into its event table.

The capture is a Postgres trigger (django-pghistory), not a Django signal, so a `.update()`, a
`bulk_update`, raw SQL and a data migration all produce version rows too. That is the invariant
the lineage graph rests on (`apps/core/lineage.py`): a missing version row is not a gap in an
audit log, it is an edge that silently misattributes data.

    from apps.core.history import tracked

    @tracked
    class Dataset(OwnedModel):
        ...

`@tracked` generates `DatasetEvent` (reachable as `Dataset.pgh_event_model`, and as
`dataset.events`), a table mirroring every column plus:

  pgh_id         UUIDv7 primary key — the identity a lineage edge points at
  pgh_created_at when the row was written
  pgh_label      "insert" or "update" (there is no delete tracker: deletes are soft, so they
                 arrive as updates, and hard deletes are blocked — apps/core/models.py)
  pgh_obj_id     the tracked row (unconstrained FK: event rows outlive nothing, but they must
                 never be the reason a write fails)
  pgh_context_id groups everything one request or task wrote, so a save that touched three
                 tables renders as one revision
  pgh_schema     which field set the row was written under (see SCHEMA_TAG)
  pgh_archive    values of fields that have since been dropped (see "Schema evolution")

Event tables are append-only (`PGHISTORY_APPEND_ONLY`), carry the owner column of the model they
mirror, and get the same row-level security policy as that model (`apps/core/rls.py`) — history
is tenant data.

## Reading a version back

`obj.history()` (`apps/core/models.py::VersionedModel`) hands those rows over as `Version`
objects, oldest first, and `to_object()` turns one back into the type it is a version *of*:

    document.history()[0].to_object()   # a Document, as it was created — typed, unsaved

That is the only interface application code needs; the generated event model stays an
implementation detail of this module. A version also knows the lineage around it —
`version.sources()` / `version.derived()`, and `is_current()` for "has this been superseded"
(`apps/core/lineage.py`).

## Schema evolution

`SCHEMA_TAG` names the tracked field set. Bump it in the same change that adds or removes a
tracked field anywhere, and regenerate `history_schema.json` (`apps/core/tests/test_history.py`
fails otherwise). It is what lets the revision page say "not tracked at this version" instead of
rendering a backfilled default as though it were real data.

The tag is the `db_default` of `pgh_schema` on *every* generated event table, so bumping it needs
`makemigrations` across all apps with tracked models — not only the app whose field changed.
Miss one and its rows keep claiming the old tag while the log says otherwise.

Adding a field: the mirrored column is added and old rows get the default. Bump the tag.
Dropping one: archive it into `pgh_archive` in a data migration that runs *before* the
`RemoveField`, then drop, then bump the tag:

    UPDATE datasets_datasetevent
       SET pgh_archive = coalesce(pgh_archive, '{}'::jsonb)
                         || jsonb_build_object('legacy_slug', legacy_slug)
     WHERE legacy_slug IS NOT NULL;

The archive is frozen at write time, so it never needs migrating again.

## Context

`pghistory_context` is one shared table with no owner column, and its upsert function needs
SELECT and UPDATE on it, so it cannot be hidden behind row-level security: **every tenant can
read every context row.** Nothing tenant-identifying goes in the metadata — no user id, no
resolved URL (`/api/datasets/<uuid>` names another tenant's object). Who acted is already on the
event row: `owner_id`, properly isolated. The metadata only says where a change came from
("api"/"task"/"command"), which is what the revision page groups by anyway.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, cast, overload

import pghistory
import pghistory.models
import pgtrigger
from django.conf import settings
from django.db import connection, models
from django.db.models import Func
from django.db.models.fields.files import FieldFile
from pghistory import runtime

from config.env import BASE_DIR

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.http.response import HttpResponseBase

    from apps.core.models import VersionedModel

# The tracked field set this code writes. Bump on every change to a tracked model's fields.
SCHEMA_TAG = "2026-09"
# Checked-in snapshot of that field set (manage.py history_schema --write).
SCHEMA_FILE = BASE_DIR / "history_schema.json"

# Models that inherit VersionedModel but are deliberately not versioned ("app_label.Model").
# Every entry needs a reason: history is the default, opting out is the exception.
HISTORY_EXEMPT = {
    # Rotated on every token refresh and purged on login: high-churn session bookkeeping with no
    # lineage value, and the rows hold credentials we would rather not keep copies of.
    "accounts.RefreshToken",
    "accounts.ApiToken",  # ditto — a credential's history is the credential.
    # Append-only and immutable by construction; it *is* the lineage graph, not a subject of it.
    "core.Lineage",
    # An operational log of what was run (apps/core/usage.py). Rows are written once and never
    # edited, so a version chain over them would only ever mirror the insert.
    "core.CommandRun",
}


class Event(pghistory.models.Event):
    """Base for every generated event model.

    Named `Event` on purpose: pghistory derives both the event model name and the reverse
    accessor from this class (`DatasetEvent`, `dataset.events`). Renaming it renames every
    generated model and the accessor with it.
    """

    # pghistory's own default is an AutoField. UUIDv7 keeps history consistent with the rest of
    # the project (NOTES.md §6) and makes a lineage edge a globally meaningful pointer.
    # `db_default` rather than a Python default because the *trigger* inserts these rows: it
    # omits the column from its INSERT, which is exactly what makes the database default fire.
    # Overriding a field of an abstract base is legal in Django (and the spike confirms the
    # trigger fills it); django-stubs only sees pghistory's AutoField declaration.
    pgh_id = models.UUIDField(  # type: ignore[assignment]
        primary_key=True, db_default=Func(function="uuidv7")
    )
    pgh_schema = models.TextField(db_default=SCHEMA_TAG)
    pgh_archive = models.JSONField(null=True, default=None)

    class Meta:
        abstract = True


def tracked[ModelT: type[models.Model]](model: ModelT) -> ModelT:
    """Class decorator: capture every insert and update of `model` in an event table.

    Must be applied to a concrete model. `pghistory.track` on an *abstract* base is accepted
    silently and then generates one concrete event model pointing at the abstract class
    (`fields.E300`) while installing no triggers at all on the subclasses — so every model
    spells this out. `apps/core/tests/test_history.py` checks that none was forgotten.
    """
    if model._meta.abstract:
        raise TypeError(
            f"@tracked cannot be applied to the abstract model {model.__name__}: pghistory "
            "installs triggers on a table, and an abstract model has none. Decorate the "
            "concrete subclasses instead."
        )
    # `track()` returns the class unchanged; going through it directly would erase the model's
    # type (the library leaves the decorator's return unannotated). The base model comes from
    # `PGHISTORY_BASE_MODEL` (config/settings.py) rather than an argument here, so a table
    # generated any other way — a third-party `pghistory.track`, a migration — gets the same
    # column set. The aggregate admin page unions those tables and depends on it.
    pghistory.track()(model)
    return model


class EventRow(Protocol):
    """The runtime shape of a generated event row: pghistory's own columns plus the mirrored
    `VersionedModel` ones.

    Event models are built at import time from the tracked model's fields, so no type checker
    can see them. This protocol is how the rest of the code says what it expects; `as_event_row`
    is the one place that asserts it.
    """

    pgh_id: uuid.UUID
    pgh_label: str
    pgh_created_at: datetime
    pgh_obj_id: uuid.UUID
    pgh_context_id: uuid.UUID | None
    pgh_schema: str
    pgh_archive: dict[str, Any] | None

    id: uuid.UUID
    owner_id: uuid.UUID
    version: int
    created: datetime
    modified: datetime
    deleted_at: datetime | None


def as_event_row(row: models.Model) -> EventRow:
    """Read a row from an event table as an `EventRow` (see the protocol's docstring)."""
    return cast(EventRow, row)


class NotTracked(LookupError):
    """The model has no event table: it is not versioned (see `tracked` and HISTORY_EXEMPT)."""


class MissingVersion(RuntimeError):
    """A tracked row has no version row — the capture trigger is not installed."""


def event_rows(model: type[models.Model], obj_pk: uuid.UUID) -> models.QuerySet[models.Model]:
    """Every version row of one object, newest version first.

    The filter is spelled as a dict because `pgh_obj_id` exists only on the generated subclass:
    named literally it would be resolved against the abstract base and fail to type-check.
    """
    event_model = event_model_for(model)
    if event_model is None:
        raise NotTracked(
            f"{model.__name__} is not versioned. Decorate it with @tracked "
            "(apps/core/history.py) or list it in HISTORY_EXEMPT."
        )
    return event_model._base_manager.filter(**{"pgh_obj_id": obj_pk}).order_by("-version")


def current_event(obj: models.Model) -> EventRow:
    """The event row representing `obj`'s state right now.

    Inside the writing transaction this already exists: the trigger fired on the INSERT/UPDATE,
    not at commit. Ordered by `version`, never by time — two writes in one transaction share a
    `pgh_created_at`.
    """
    event = event_rows(type(obj), obj.pk).first()
    if event is None:
        raise MissingVersion(f"{obj} has no version row; is its capture trigger installed?")
    return as_event_row(event)


@dataclass(frozen=True, repr=False)
class Version[ModelT: models.Model]:
    """One past state of one row, wearing the type of the model it is a state of.

    An event row is a generated class nobody imports and mypy cannot see (`EventRow`). This is
    the way back: `to_object()` rebuilds the tracked model's own type from it, so reading an old
    state needs no knowledge of the event table at all.

        first = document.history()[0]      # Version[Document]
        first.version, first.at            # 1, when it was written
        first.to_object().name             # a Document — typed, and as it was created

    The event row stays reachable as `.event` for the pghistory columns (`pgh_label`,
    `pgh_context_id`, `pgh_archive`) and for the mirrored fields as they were stored.
    """

    #: The tracked model — `Document`, never `DocumentEvent`.
    model: type[ModelT]
    #: The row this version was read from.
    event: EventRow

    @property
    def version(self) -> int:
        """Position in the version chain; 1 is the insert (`VersionedModel.version`)."""
        return self.event.version

    @property
    def at(self) -> datetime:
        """When this version was written. Two versions of one transaction share the value."""
        return self.event.pgh_created_at

    @property
    def deleted(self) -> bool:
        """The row was soft-deleted as of this version."""
        return self.event.deleted_at is not None

    @property
    def object_id(self) -> uuid.UUID:
        """The row this is a version of — how to reach the live one."""
        return self.event.pgh_obj_id

    def is_current(self) -> bool:
        """Is this still the row's latest version, or has it moved on since?

        The second case is exactly what makes a lineage edge stale, so a source version that
        answers False is a derived row that would come out differently if it were rebuilt
        (`lineage.stale_derivations`). One query; reads through `_base_manager`, so a
        soft-deleted row still answers.
        """
        latest = (
            self.model._base_manager.filter(pk=self.object_id)
            .values_list("version", flat=True)
            .first()
        )
        return bool(latest == self.version)

    @overload
    def sources(self) -> list[Version[VersionedModel]]: ...

    @overload
    def sources[SourceT: VersionedModel](self, model: type[SourceT]) -> list[Version[SourceT]]: ...

    def sources(self, model: type[VersionedModel] | None = None) -> list[Version[Any]]:
        """The versions this one was built from (`apps/core/lineage.py`).

        What *this* version consumed, which is where an edge is recorded;
        `VersionedModel.sources()` asks the wider "what was this row ever built from", and
        documents `model`.
        """
        from apps.core import lineage  # noqa: PLC0415 - lineage is layered on top of this module

        # Oldest edge first, like everywhere else here; `sources_of_version` keeps
        # `Lineage.Meta.ordering` (newest first), which the revision page wants.
        edges = lineage.sources_of_version(self.event).order_by("created", "id")
        found = lineage.source_versions(edges)
        return found if model is None else [v for v in found if issubclass(v.model, model)]

    @overload
    def derived(self) -> list[Version[VersionedModel]]: ...

    @overload
    def derived[TargetT: VersionedModel](self, model: type[TargetT]) -> list[Version[TargetT]]: ...

    def derived(self, model: type[VersionedModel] | None = None) -> list[Version[Any]]:
        """The versions of other rows that were built from this exact version."""
        from apps.core import lineage  # noqa: PLC0415 - lineage is layered on top of this module

        found = lineage.target_versions(lineage.derived_from_version(self.event))
        return found if model is None else [v for v in found if issubclass(v.model, model)]

    def untracked_fields(self) -> frozenset[str]:
        """Fields `to_object()` cannot speak for at this version.

        A field added later exists on the event table for every row, holding whatever the
        column was backfilled with — presenting that as the value the object had would be a
        lie (see "Schema evolution" and `apps/core/revisions.py`). A version older than the
        schema log itself can vouch for nothing, so every field is named.
        """
        current = frozenset(field.name for field in self.model._meta.concrete_fields)
        tracked_then = fields_at(self.event.pgh_schema, self.model._meta.label)
        return current if tracked_then is None else current - tracked_then

    def to_object(self) -> ModelT:
        """The row as it stood at this version, as an unsaved instance of `model`.

        Detached from the database on purpose: this is a past state, and the live row has
        usually moved on. Saving it back is a restore and a perfectly normal write — the
        `bump_version` trigger gives it the *next* version number rather than resurrecting the
        old one, and the event table gains a row saying the restore happened.
        """
        row = cast(models.Model, self.event)
        values: dict[str, Any] = {}
        for field in self.model._meta.concrete_fields:
            if not hasattr(row, field.attname):
                continue  # dropped from tracking; the old value is in `pgh_archive`
            value = getattr(row, field.attname)
            # A FileField hands out a FieldFile bound to the *event* model's field. The stored
            # key is what the tracked model's own descriptor wants.
            values[field.attname] = value.name if isinstance(value, FieldFile) else value
        obj = self.model(**values)
        # Not a new row: a `save()` must UPDATE the row this version belongs to. Left as
        # "adding", Django would force an INSERT (the pk has a database default) and the
        # restore would fail on a duplicate key.
        obj._state.adding = False
        return obj

    def __repr__(self) -> str:
        # An event row mirrors `LABEL_FIELDS` but cannot inherit `VersionedModel.__str__`, so
        # naming the row is spelled out here. Reading the row itself, not `to_object()`: a
        # `__repr__` that builds a model instance is a `__repr__` that can raise.
        from apps.core.models import LABEL_FIELDS  # noqa: PLC0415 - models is layered on this one

        values = (getattr(self.event, field, None) for field in LABEL_FIELDS)
        name = next((str(value) for value in values if value), "")
        shown = f' "{name}"' if name else ""
        return f"<Version {self.model.__name__}{shown} v{self.version} {self.at:%Y-%m-%d %H:%M}>"


def versions[ModelT: models.Model](model: type[ModelT], obj_pk: uuid.UUID) -> list[Version[ModelT]]:
    """Every version of one row, oldest first. `VersionedModel.history()` is the front door."""
    rows = event_rows(model, obj_pk).order_by("version")
    return [Version(model, as_event_row(row)) for row in rows]


def tracked_models() -> list[tuple[type[models.Model], type[pghistory.models.Event]]]:
    """Every model that is versioned, paired with its event model, in a stable order."""
    from django.apps import apps  # noqa: PLC0415 - registry is not ready at import time

    pairs = [
        (model, event_model)
        for model in apps.get_models()
        if (event_model := event_model_for(model)) is not None
    ]
    return sorted(pairs, key=lambda pair: pair[0]._meta.label)


def tracked_fields() -> dict[str, list[str]]:
    """The tracked field set of every versioned model, right now."""
    return {
        model._meta.label: sorted(
            field.name for field in event_model._meta.fields if not field.name.startswith("pgh_")
        )
        for model, event_model in tracked_models()
    }


def load_schema_log() -> dict[str, Any]:
    """The checked-in `history_schema.json`, or an empty log if it does not exist yet."""
    if not SCHEMA_FILE.exists():  # pragma: no cover - only before the first --write
        return {"current": SCHEMA_TAG, "tags": {}}
    log: dict[str, Any] = json.loads(SCHEMA_FILE.read_text())
    return log


def tracked_schema() -> dict[str, Any]:
    """The schema log with the current tag folded in.

    A *log*, not a snapshot: every tag keeps the field set it named, so a version row written
    under an older tag can still be read correctly (`fields_at`). Older entries are never
    rewritten — that is the whole value of them.

    `apps/core/tests/test_history.py` compares this against the checked-in file, so adding or
    removing a tracked field cannot land without bumping `SCHEMA_TAG` and regenerating.
    """
    log = load_schema_log()
    tags = {**log.get("tags", {}), SCHEMA_TAG: tracked_fields()}
    return {"current": SCHEMA_TAG, "tags": dict(sorted(tags.items()))}


def fields_at(tag: str, label: str) -> frozenset[str] | None:
    """Which fields of `label` were tracked under schema tag `tag`; None if the tag is unknown.

    An unknown tag means a row older than the log: the caller must say "not known at this
    version" rather than render a backfilled default as if it were data.
    """
    entry = load_schema_log().get("tags", {}).get(tag)
    if entry is None or label not in entry:
        return None
    fields: list[str] = entry[label]
    return frozenset(fields)


def event_models() -> list[type[pghistory.models.Event]]:
    """Every concrete event table, in a stable order."""
    from django.apps import apps  # noqa: PLC0415 - registry is not ready at import time

    found = [
        model
        for model in apps.get_models()
        if issubclass(model, pghistory.models.Event) and not model._meta.abstract
    ]
    return sorted(found, key=lambda model: model._meta.db_table)


def event_model_for(model: type[models.Model]) -> type[pghistory.models.Event] | None:
    """The event model that tracks `model`, or None if it is untracked.

    `hasattr(model, "pgh_event_model")` is not enough: the attribute is inherited, so a subclass
    of a tracked model (or of a wrongly decorated abstract base) reports its parent's table.
    """
    event_model: type[pghistory.models.Event] | None = getattr(model, "pgh_event_model", None)
    if event_model is None:
        return None
    # Looked up by iteration rather than `_meta.get_field("pgh_obj")`: the field only exists on
    # the *generated* subclass, so a literal name cannot type-check against the abstract base.
    obj_field = next((f for f in event_model._meta.fields if f.name == "pgh_obj"), None)
    if obj_field is None:
        return None
    return event_model if obj_field.related_model is model else None


# --- Escape hatches: the two places the triggers must not apply -------------------------------

# apps/core/models.py::VersionedModel.Meta
PROTECT_TRIGGER = "no_hard_delete"
VERSION_TRIGGER = "bump_version"
# Generated: pghistory's own capture triggers and the append-only guard on event tables.
CAPTURE_TRIGGERS = ("insert_insert", "update_update")
APPEND_ONLY_TRIGGER = "append_only"


def _trigger_uris(*names: str) -> list[str]:
    """pgtrigger URIs (`app_label.Model:trigger`) of every registered trigger with these names."""
    return [
        f"{model._meta.app_label}.{model._meta.object_name}:{trigger.name}"
        for model, trigger in pgtrigger.registry.registered()
        if trigger.name in names
    ]


@contextmanager
def hard_delete() -> Iterator[None]:
    """Really remove rows, history included — the deliberate exception to "nothing is deleted".

    Two callers, both of which mean it: erasing a tenant (`apps/core/tenants.py`, where leaving
    the version rows behind would keep the erased user's data forever) and test teardown. Lifts
    exactly the two delete guards, so a stray UPDATE inside the block is still versioned.
    """
    with pgtrigger.ignore(*_trigger_uris(PROTECT_TRIGGER, APPEND_ONLY_TRIGGER)):
        yield


@contextmanager
def unversioned() -> Iterator[None]:
    """Write rows exactly as given: no version bump, no event capture, event tables writable.

    For `loaddata` only (`apps/core/backups.py`). A restore replays rows that already carry
    their `version` and their event rows; without this the triggers would bump every restored
    version, write a second event row for each, and then fail outright on an event row that
    already exists (append-only rejects the UPDATE).
    """
    uris = _trigger_uris(VERSION_TRIGGER, APPEND_ONLY_TRIGGER, PROTECT_TRIGGER, *CAPTURE_TRIGGERS)
    with pgtrigger.ignore(*uris):
        yield


# --- Context: where a change came from --------------------------------------------------------


def _check_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Refuse anything tenant-identifying — the context table is readable by every tenant."""
    for key, value in metadata.items():
        text = str(value)
        try:
            uuid.UUID(text)
        except ValueError:
            continue
        raise ValueError(
            f"history context {key}={text!r} looks like an identifier. pghistory_context is a "
            "shared table every tenant can read; keep object and user ids out of it (the event "
            "row's owner_id already says whose change it was)."
        )
    return metadata


def _forget_injected_context() -> None:
    """Stop attributing later writes to the step that just ended.

    pghistory injects its context with `set_config(..., true)` — **transaction-local** — and only
    re-injects before a write while a context is open. So the last label set inside a transaction
    outlives its block: with `tenant_context` holding one transaction open around a whole request
    or command, the next unlabelled write would be recorded under a step that had already
    finished. Blanking the setting makes the trigger's cast to `uuid` fail into its own
    `EXCEPTION WHEN OTHERS` and return NULL, which is precisely "this write had no context".

    Only the outermost block needs this: a nested one restores the outer context, and the next
    write injects that.
    """
    if not connection.in_atomic_block:
        return  # the transaction is over; the setting went with it
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('pghistory.context_id', '', true), "
            "set_config('pghistory.context_metadata', '', true)"
        )


@contextmanager
def history_context(source: str, **metadata: str) -> Iterator[None]:
    """Group everything written inside the block under one context id.

    Background jobs and management commands must open one explicitly, or their version rows are
    attributed to nothing at all — and with most work happening off-request here, that is the
    common path rather than the edge case. Wrap the entrypoint; a finer step inside it gets its
    own block, or names itself on the write (`save(context="…")`, `apps/core/models.py`).

    **Nesting opens a new context, it does not merge.** pghistory's own `context()` folds a nested
    call's metadata into the active context — so a step labelled inside a request would relabel
    every write of that request. A step is its own run: inside an outer block this swaps in a
    fresh context for the duration and puts the outer one back afterwards. The hook pghistory
    installed for the outer block reads `_tracker.value` at execute time, so the swap is all it
    takes; the context row itself is upserted by the first tracked write, as always.
    """
    checked = _check_metadata(metadata)
    outer = getattr(runtime._tracker, "value", None)
    if outer is None:
        try:
            with pghistory.context(source=source, **checked):
                yield
        finally:
            _forget_injected_context()
        return

    runtime._tracker.value = runtime.Context(
        id=uuid.uuid4(), metadata={"source": source, **checked}
    )
    try:
        yield
    finally:
        runtime._tracker.value = outer


class HistoryMiddleware:
    """Opens one context per write request, holding no tenant-identifying data (see module docs).

    Deliberately *not* pghistory's own `HistoryMiddleware`: that one records the URL and swaps in
    a request class that adds the user id to the context the moment `request.user` is assigned —
    which `TenantMiddleware` does on every authenticated API request. Both would land in a table
    every tenant can read.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        if request.method not in settings.PGHISTORY_MIDDLEWARE_METHODS:
            return self.get_response(request)
        with history_context("api", method=request.method or ""):
            return self.get_response(request)
