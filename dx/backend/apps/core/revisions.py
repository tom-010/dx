"""Reading history back: what changed, when, and what it was built from.

The revision page's data layer. Three things make it more than "list the event rows":

**Diffs are computed against the previous version**, not stored. Event rows are full snapshots,
so the change set is `previous → current` over the mirrored fields.

**Diffs are schema-aware.** Each row records the `pgh_schema` tag it was written under
(`apps/core/history.py`). If a field was added later, older rows hold a backfilled default that
the object never actually had; presenting that as data would be a lie. Fields that were not
tracked at both ends of a comparison are reported separately as `unknown`, never as a change.

**Revisions group by `pgh_context`.** One save that touched three tables is one revision, not
three — that is what the context id is for.

Values are rendered as text here on purpose: a revision page displays them, and a typed union
of "whatever any field of any model can hold" is not a contract worth generating a client from.
The underlying event rows stay queryable for anything that needs real values.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import pghistory.models
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Max
from django.db.models.fields.reverse_related import ForeignObjectRel
from pydantic import BaseModel as PydanticModel

from apps.core import lineage
from apps.core.history import (
    EventRow,
    as_event_row,
    event_model_for,
    event_rows,
    fields_at,
    tracked_models,
)
from apps.core.models import LABEL_FIELDS, OWNER_COLUMN, OwnedModel

# Columns every row has and nobody wants in a diff: they change on every single write.
NOISE = frozenset({"id", "created", "modified", "version", "owner"})


@dataclass(frozen=True)
class Change:
    """One field that differs between two consecutive versions."""

    field: str
    old: str | None
    new: str | None


@dataclass(frozen=True)
class SourceRef:
    """A lineage edge feeding this version: the exact source version it consumed."""

    model: str
    label: str
    object_id: uuid.UUID
    version: int
    pgh_id: uuid.UUID
    is_stale: bool


@dataclass(frozen=True)
class Revision:
    """One version of one row — the object itself, or a child row written in the same save."""

    pgh_id: uuid.UUID
    object_id: uuid.UUID
    model: str
    version: int
    label: str
    at: datetime
    schema_tag: str
    schema_known: bool
    deleted: bool
    changes: list[Change]
    #: Fields that were not tracked at both ends of the comparison — shown as "not recorded at
    #: this version" rather than as a change from a value that was never there.
    unknown_fields: list[str]
    #: Values of fields that have since been dropped, archived at write time (pgh_archive).
    archived: dict[str, str]
    context_id: uuid.UUID | None
    sources: list[SourceRef] = field(default_factory=list)
    #: False for the object's own versions; True for a child row (a tag link, say) written in
    #: the same save. Children are described, not diffed: their columns are foreign keys, and a
    #: page full of UUIDs says less than "tag 'sales'".
    is_related: bool = False
    #: What the child row points at, in words — empty for the object's own versions.
    description: str = ""


@dataclass(frozen=True)
class RevisionGroup:
    """Everything one request or task wrote, as the page shows it: a single revision."""

    context_id: uuid.UUID | None
    source: str
    at: datetime
    revisions: list[Revision]


def resource_models() -> dict[str, type[OwnedModel]]:
    """URL segment -> versioned model, derived from the registry rather than a hand-kept list.

    The key is the model name in lower case (`dataset`, `mediaitem`). Only owned models are
    addressable: they are the ones a user can be shown their own history of.
    `apps/core/tests/test_history.py` checks the names stay unique.
    """
    return {
        model._meta.model_name: model
        for model, _event_model in tracked_models()
        if issubclass(model, OwnedModel) and model._meta.model_name is not None
    }


def resource_model(resource: str) -> type[OwnedModel] | None:
    return resource_models().get(resource.lower())


def _render(value: object) -> str | None:
    """A field value as the revision page shows it.

    Typed JSON columns (`django_pydantic_field`) come back as pydantic instances whose `str()`
    is a Python repr; JSON is what a reader of a diff expects, and it also compares stably.
    """
    if value is None:
        return None
    if isinstance(value, PydanticModel):
        return value.model_dump_json()
    if isinstance(value, dict | list):
        return json.dumps(value, default=str, sort_keys=True)
    return str(value)


def _comparable(model_label: str, row: EventRow) -> frozenset[str] | None:
    tracked = fields_at(row.pgh_schema, model_label)
    return None if tracked is None else tracked - NOISE


def diff(
    model_label: str, previous: EventRow | None, current: EventRow
) -> tuple[list[Change], list[str]]:
    """Changes from `previous` to `current`, plus the fields neither row can speak for.

    `previous is None` means the insert: every non-empty field is reported as newly set, which
    is what a revision page wants to show for the first version.
    """
    now_fields = _comparable(model_label, current)
    if now_fields is None:
        # The row predates the schema log: we cannot tell which fields it actually held.
        return [], ["*"]

    if previous is None:
        changes = [
            Change(field=name, old=None, new=_render(getattr(current, name, None)))
            for name in sorted(now_fields)
            if getattr(current, name, None) not in (None, "", 0)
        ]
        return changes, []

    then_fields = _comparable(model_label, previous)
    if then_fields is None:
        return [], sorted(now_fields)

    unknown = sorted(now_fields ^ then_fields)
    changes = []
    for name in sorted(now_fields & then_fields):
        old, new = getattr(previous, name, None), getattr(current, name, None)
        if old != new:
            changes.append(Change(field=name, old=_render(old), new=_render(new)))
    return changes, unknown


def _tracked_name(event_model: type[models.Model] | None) -> str:
    """The name of the model an event table mirrors — "Dataset", not "DatasetEvent"."""
    tracked = getattr(event_model, "pgh_tracked_model", None)
    if tracked is not None:
        return str(tracked.__name__)
    return event_model.__name__ if event_model is not None else "unknown"  # pragma: no cover


def _latest_versions(edges: list[lineage.Lineage]) -> dict[tuple[int, uuid.UUID], int]:
    """Highest recorded version per source object, one query per source table.

    Read from the *event* table rather than the live row: it answers the same question, works
    for a source that has since been soft-deleted, and needs no second model lookup.
    """
    by_type: dict[int, set[uuid.UUID]] = {}
    for edge in edges:
        by_type.setdefault(edge.source_type_id, set()).add(edge.source_obj_id)

    latest: dict[tuple[int, uuid.UUID], int] = {}
    for type_id, object_ids in by_type.items():
        event_model = ContentType.objects.get_for_id(type_id).model_class()
        if event_model is None:  # pragma: no cover - a content type without a model
            continue
        rows = (
            event_model._base_manager.filter(**{"pgh_obj_id__in": sorted(object_ids)})
            .values_list("pgh_obj_id")
            .annotate(highest=Max("version"))
        )
        for object_id, highest in rows:
            latest[(type_id, object_id)] = highest
    return latest


def row_label(row: object, fallback: object) -> str:
    """Name a row the way `BaseModel.__str__` would.

    Event rows are generated classes that do not inherit `BaseModel`, so they cannot borrow its
    `__str__` — but they mirror the same columns, so the same preference works on them.
    """
    for name in LABEL_FIELDS:
        value = getattr(row, name, None)
        if value:
            return str(value)
    return str(fallback)


def _source_refs(target: EventRow) -> list[SourceRef]:
    edges = list(lineage.sources_of_version(target))
    latest = _latest_versions(edges)
    refs = []
    for edge in edges:
        try:
            label = row_label(edge.resolve_source(), edge.source_obj_id)
        except models.ObjectDoesNotExist:  # pragma: no cover - append-only, should not happen
            label = str(edge.source_obj_id)
        refs.append(
            SourceRef(
                model=_tracked_name(edge.source_type.model_class()),
                label=label,
                object_id=edge.source_obj_id,
                version=edge.source_version,
                pgh_id=edge.source_pgh_id,
                # "Built from a version that has since been superseded" — the question the
                # denormalised columns exist to answer (apps/core/lineage.py).
                is_stale=edge.source_version
                < latest.get((edge.source_type_id, edge.source_obj_id), edge.source_version),
            )
        )
    return refs


def child_relations(model: type[models.Model]) -> list[ForeignObjectRel]:
    """Reverse foreign keys from tracked, owned rows that belong to `model`.

    These are the rows a save can touch alongside the object itself — an explicit m2m through
    model is exactly this shape — and they are what makes a revision span tables.
    """
    return [
        rel
        for rel in model._meta.related_objects
        # `one_to_many`: the reverse side of a ForeignKey pointing at `model`. (`DatasetEvent`
        # shows up here too, via pgh_obj — the OwnedModel check is what leaves it out.)
        if rel.one_to_many
        and issubclass(rel.related_model, OwnedModel)
        and event_model_for(rel.related_model) is not None
    ]


def _describe(row: models.Model, skip: set[str]) -> str:
    """A child event row in words: what its other foreign keys point at, resolved to live rows.

    `DatasetTagEvent(dataset_id=…, tag_id=…)` for a dataset becomes "sales". Resolution goes
    through `_base_manager`, so a soft-deleted target still has a name.
    """
    labels = []
    for column in type(row)._meta.fields:
        if not column.is_relation or column.attname in skip or column.name.startswith("pgh_"):
            continue
        target = column.related_model
        value = getattr(row, column.attname, None)
        if target is None or value is None:
            continue
        found = target._base_manager.filter(pk=value).first()
        if found is not None:
            labels.append(str(found))
    return ", ".join(labels)


def _related_revisions(obj: OwnedModel) -> list[Revision]:
    revisions = []
    for rel in child_relations(type(obj)):
        related = rel.related_model
        event_model = event_model_for(related)
        assert event_model is not None  # child_relations filtered these already
        skip = {rel.field.attname, OWNER_COLUMN}
        rows = event_model._base_manager.filter(**{rel.field.attname: obj.pk}).order_by(
            "pgh_created_at", "version"
        )
        for row in rows:
            event = as_event_row(row)
            revisions.append(
                Revision(
                    pgh_id=event.pgh_id,
                    object_id=event.id,
                    model=related.__name__,
                    version=event.version,
                    label=event.pgh_label,
                    at=event.pgh_created_at,
                    schema_tag=event.pgh_schema,
                    schema_known=fields_at(event.pgh_schema, related._meta.label) is not None,
                    deleted=event.deleted_at is not None,
                    changes=[],
                    unknown_fields=[],
                    archived={k: str(v) for k, v in (event.pgh_archive or {}).items()},
                    context_id=event.pgh_context_id,
                    sources=[],
                    is_related=True,
                    description=_describe(row, skip),
                )
            )
    return revisions


def revisions_of(obj: OwnedModel) -> list[Revision]:
    """Every version of `obj` and of the child rows written with it, newest first.

    Child rows are what makes one save render as one revision even when it touched several
    tables: a PATCH that renames a dataset and adds a tag writes `DatasetEvent` *and*
    `DatasetTagEvent`, and `group_by_context` folds them back together.
    """
    model = type(obj)
    label = model._meta.label
    rows = [as_event_row(row) for row in event_rows(model, obj.pk)]
    # Oldest first while diffing: each row is compared with the one before it.
    ordered = list(reversed(rows))

    result = []
    for index, row in enumerate(ordered):
        previous = ordered[index - 1] if index else None
        changes, unknown = diff(label, previous, row)
        result.append(
            Revision(
                pgh_id=row.pgh_id,
                object_id=row.id,
                model=model.__name__,
                version=row.version,
                label=row.pgh_label,
                at=row.pgh_created_at,
                schema_tag=row.pgh_schema,
                schema_known=fields_at(row.pgh_schema, label) is not None,
                deleted=row.deleted_at is not None,
                changes=changes,
                unknown_fields=unknown,
                archived={k: str(v) for k, v in (row.pgh_archive or {}).items()},
                context_id=row.pgh_context_id,
                sources=_source_refs(row),
            )
        )
    result += _related_revisions(obj)
    # Newest first, by `pgh_id` rather than by time: every row one request wrote carries the
    # same `pgh_created_at` (the trigger stamps transaction time), while the ids are UUIDv7 and
    # therefore in write order. `group_by_context` orders within a save.
    result.sort(key=lambda revision: (revision.at, revision.pgh_id), reverse=True)
    return result


def group_by_context(revisions: list[Revision]) -> list[RevisionGroup]:
    """Fold per-object versions into the saves that produced them.

    Rows written without a context (a shell, a migration, a job that forgot to open one) each
    become their own group: they are genuinely unrelated, and pretending otherwise would merge
    changes that never happened together.
    """
    groups: dict[object, list[Revision]] = {}
    for index, revision in enumerate(revisions):
        key = revision.context_id if revision.context_id is not None else ("orphan", index)
        groups.setdefault(key, []).append(revision)

    sources = context_sources({r.context_id for r in revisions if r.context_id})
    return [
        RevisionGroup(
            context_id=(context_id := members[0].context_id),
            source="unknown" if context_id is None else sources.get(context_id, "unknown"),
            at=max(member.at for member in members),
            # The object's own revision leads, then the child rows in the order they were
            # written — "renamed it, and added this tag" reads the way it happened.
            revisions=sorted(members, key=lambda r: (r.is_related, r.pgh_id)),
        )
        for members in groups.values()
    ]


def context_sources(context_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Where each context came from ("api", "task", ...) — never anything tenant-identifying.

    `pghistory_context` is a shared table, so only the non-identifying `source` key is read
    back; see the "Context" section of `apps/core/history.py`.
    """
    if not context_ids:
        return {}
    rows = pghistory.models.Context.objects.filter(pk__in=context_ids)
    return {row.pk: str(row.metadata.get("source", "unknown")) for row in rows}
