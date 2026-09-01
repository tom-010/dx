"""Data lineage: which *version* of which row a derived row was built from.

A foreign key answers "who is the author now". A lineage edge answers "which state of the
author was this document built from" — and those are different questions the moment anything is
edited. When the source is renamed, the FK follows and the edge does not. That is the whole
point of pointing at an event row (`pgh_id`, `apps/core/history.py`) instead of at the live row.

    with acting_as(user):
        summary = create_summary(user, source=dataset)
        record_derivation(summary, sources=[dataset])

    # What did this summary actually consume? `VersionedModel.sources()` is the front door:
    # it hands back `history.Version` objects, so a source wears the type of the model it is a
    # version of instead of being an untyped event row. Naming that model types the call.
    summary.sources(Dataset)[0].to_object().name   # the source's name as it stood then
    summary.sources(Dataset)[0].is_current()       # False once the dataset has moved on
    dataset.derived(Summary)                       # the same edges, read the other way

    # The edges themselves — what the denormalised columns are for
    sources_of(summary)         # feeding the version `summary` is at right now
    derived_from(dataset)       # everything ever derived from any version of it
    stale_derivations(dataset)  # ...and what would come out differently if rebuilt now

`Lineage` is deliberately **not** a `BaseModel`: it has no version chain of its own, it is
append-only (a graph whose nodes can be edited is not a graph), and it is never soft-deleted.
It does carry `owner`, so `apps/core/rls.py` gives it the same tenant policy as everything else
— generic pointers offer no structural protection against a cross-tenant reference, so the
database has to say no.

There is no foreign key on `source_pgh_id`/`target_pgh_id`, by design: event rows are
append-only and nothing hard-deletes them, so a dangling pointer cannot arise, and an FK into an
event table would make a *write* fail for the sake of a *reference*. pghistory uses
`db_constraint=False` on its own `pgh_obj` for the same reason.
"""

from __future__ import annotations

import subprocess
import traceback
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from typing import TYPE_CHECKING, cast

import pgtrigger
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Func
from django.db.models.functions import Now
from django_pydantic_field import SchemaField
from pydantic import BaseModel as PydanticModel
from pydantic import ConfigDict

from apps.core.db import NoTenantContext, current_user_id
from apps.core.history import (
    EventRow,
    NotTracked,
    Version,
    as_event_row,
    current_event,
    event_model_for,
)
from config.env import BASE_DIR

if TYPE_CHECKING:
    from apps.core.models import VersionedModel


class LineageError(Exception):
    """A derivation cannot be recorded as stated."""


class StackFrame(PydanticModel):
    """One frame of the call stack that recorded an edge, as `traceback` reports it."""

    model_config = ConfigDict(extra="forbid")

    file: str
    line: int
    func: str
    #: The source line itself, stripped. Empty when the file is no longer readable — the stack
    #: is a record of a past run, and the code it names can be deleted or edited afterwards.
    code: str = ""

    def __str__(self) -> str:
        return f"{self.file}:{self.line} in {self.func}"


class Lineage(models.Model):
    """One edge: `target` (a version of a derived row) was built from `source` (a version of
    another row). Immutable once written."""

    id = models.UUIDField(primary_key=True, db_default=Func(function="uuidv7"), editable=False)
    owner = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="+", editable=False
    )

    target_type = models.ForeignKey(ContentType, on_delete=models.PROTECT, related_name="+")
    target_pgh_id = models.UUIDField()

    source_type = models.ForeignKey(ContentType, on_delete=models.PROTECT, related_name="+")
    source_pgh_id = models.UUIDField()
    # Denormalised from the source event row. `source_pgh_id` determines both, but "everything
    # derived from this dataset" is the common query, and without these it would have to fan out
    # to a different event table per content type before it could filter at all.
    source_obj_id = models.UUIDField()
    source_version = models.PositiveIntegerField()

    # The run that produced the edge (`pghistory_context`), for grouping on the revision page.
    pgh_context = models.UUIDField(null=True, default=None)
    #: The call stack that recorded the edge, outermost frame first — which code claimed this
    #: derivation, down to the line. A typed JSON column like any other (NOTES.md §5); frames
    #: inside `apps/core/lineage.py` are left out, so `stack[-1]` is the caller.
    stack = SchemaField(list[StackFrame], default=list)
    #: The build the stack belongs to (`APP_VERSION`: the short commit the image was built from,
    #: "dev" outside one). A file and a line only identify code together with the revision it was
    #: in, so this is stored *beside* the stack and captured in the same breath — `git show
    #: <release>:<file>` then reconstructs exactly what ran.
    release = models.CharField(max_length=64, default="", editable=False)
    created = models.DateTimeField(db_default=Now(), editable=False)

    class Meta:
        ordering = ["-created", "-id"]
        indexes = [
            models.Index(fields=["owner", "source_obj_id", "source_version"]),
            models.Index(fields=["owner", "target_pgh_id"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["target_pgh_id", "source_pgh_id"], name="uniq_lineage_edge"
            )
        ]
        triggers = [
            # The nodes of the graph are immutable; so are its edges. Not `VersionedModel`'s
            # `no_hard_delete`: erasing a tenant must still be able to remove them
            # (apps/core/history.py::hard_delete lifts exactly these two names).
            pgtrigger.Protect(name="no_hard_delete", operation=pgtrigger.Delete),
            pgtrigger.Protect(name="append_only", operation=pgtrigger.Update),
        ]

    def __str__(self) -> str:
        return f"{self.target_type.model}[{self.target_pgh_id}] <- {self.source_type.model}"

    def resolve_source(self) -> EventRow:
        """The source event row: the source's fields exactly as they stood when it was used."""
        return self._resolve(self.source_type, self.source_pgh_id)

    def resolve_target(self) -> EventRow:
        return self._resolve(self.target_type, self.target_pgh_id)

    @staticmethod
    def _resolve(content_type: ContentType, pgh_id: uuid.UUID) -> EventRow:
        model = content_type.model_class()
        if model is None:  # pragma: no cover - a content type left over from a removed model
            raise LineageError(f"content type {content_type} no longer resolves to a model")
        return as_event_row(model._base_manager.get(pgh_id=pgh_id))


def _event_type(obj: VersionedModel) -> ContentType:
    """The content type of the *event* model of `obj` — what an edge's `*_type` points at."""
    event_model = event_model_for(type(obj))
    if event_model is None:
        raise NotTracked(
            f"{type(obj).__name__} is not versioned, so nothing can be derived from it. "
            "Decorate it with @tracked (apps/core/history.py)."
        )
    return ContentType.objects.get_for_model(event_model)


def _release() -> str:
    """The build this process is running, for `Lineage.release`.

    `scripts/build.sh` stamps the short commit into the image as `APP_VERSION`, so production
    answers without ever touching git. Outside an image that value is the literal "dev", which
    would make every locally recorded edge point at an unidentifiable pile of code — so ask the
    working tree instead, once per process.
    """
    return settings.APP_VERSION if settings.APP_VERSION != "dev" else _git_release()


@cache
def _git_release() -> str:
    """The working tree's commit, `-dirty` when it has uncommitted changes.

    Cached, not read per call: `git status` walks the tree, and the answer cannot change under a
    running process in any way worth catching. The dirty marker is the honest half — a commit
    alone names code that, in a tree with local edits, never ran.

    Falls back to "dev" when git cannot answer at all (no git binary, no checkout: a container
    built from a tarball, a source distribution).
    """

    def git(*args: str) -> str:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
            ["git", *args],  # noqa: S607 - resolved via PATH on purpose: dev machines only
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()

    try:
        commit = git("rev-parse", "--short", "HEAD")
        dirty = bool(git("status", "--porcelain"))
    except OSError, subprocess.SubprocessError:
        return "dev"
    return f"{commit}-dirty" if dirty else commit


def _capture_stack() -> list[StackFrame]:
    """The call stack leading here, outermost frame first, without this module's own frames.

    `extract_stack` resolves each frame's source line through `linecache`, so the first call in
    a process touches the files on disk; afterwards it is cached. That cost buys the one thing a
    `producer` string cannot give: the exact line that asserted the derivation.
    """
    return [
        StackFrame(
            file=frame.filename,
            line=frame.lineno or 0,
            func=frame.name,
            code=(frame.line or "").strip(),
        )
        for frame in traceback.extract_stack()
        if frame.filename != __file__
    ]


def record_derivation(
    target: VersionedModel,
    sources: Sequence[VersionedModel],
    *,
    context_id: uuid.UUID | None = None,
) -> list[Lineage]:
    """Record that `target`'s current version was built from the current versions of `sources`.

    Call it in the same transaction as the write that produced `target`, so "current version" is
    the one the caller just created rather than whatever a later request leaves behind.
    """
    owner_id = current_user_id.get()
    if owner_id is None:
        raise NoTenantContext("record_derivation needs an active tenant context")

    target_event = current_event(target)
    target_type = _event_type(target)
    # Captured once: every edge of one call was recorded from the same place, by the same build.
    stack = _capture_stack()
    release = _release()
    edges = []
    for source in sources:
        source_event = current_event(source)
        edges.append(
            Lineage(
                owner_id=owner_id,
                target_type=target_type,
                target_pgh_id=target_event.pgh_id,
                source_type=_event_type(source),
                source_pgh_id=source_event.pgh_id,
                source_obj_id=source.pk,
                source_version=source_event.version,
                pgh_context=context_id or target_event.pgh_context_id,
                stack=stack,
                release=release,
            )
        )
    Lineage.objects.bulk_create(edges)
    return edges


def sources_of(target: VersionedModel) -> models.QuerySet[Lineage]:
    """The edges feeding `target`'s current version."""
    return Lineage.objects.filter(target_pgh_id=current_event(target).pgh_id)


def sources_of_version(target_event: EventRow) -> models.QuerySet[Lineage]:
    """The edges feeding one specific version — what the revision page links to."""
    return Lineage.objects.filter(target_pgh_id=target_event.pgh_id)


def derived_from(source: VersionedModel) -> models.QuerySet[Lineage]:
    """Everything ever derived from any version of `source`."""
    return Lineage.objects.filter(source_obj_id=source.pk)


# --- The two ends of an edge, as versions ------------------------------------------------------
#
# `resolve_source()` hands back an event row, which is a generated class with no type. These
# return `history.Version` instead, so the far end of an edge wears the type of the model it is
# a version of: `dataset.sources()[0].to_object()` is a `Document`.


def all_sources_of(target: VersionedModel) -> models.QuerySet[Lineage]:
    """Every edge feeding *any* version of `target` — everything it was ever built from.

    `sources_of` asks the narrower question: what the version `target` is at *right now* was
    built from. An edge names the version that consumed the source, so one later edit to
    `target` empties that set — which is what a revision page wants (the edge belongs on the
    revision it was recorded for) and never what "where did this come from" wants.
    """
    if event_model_for(type(target)) is None:
        raise NotTracked(
            f"{type(target).__name__} is not versioned, so it has no lineage. Decorate it with "
            "@tracked (apps/core/history.py)."
        )
    return Lineage.objects.filter(
        target_pgh_id__in=_version_ids(type(target), {target.pk})
    ).order_by("created", "id")


def derived_from_version(source: EventRow) -> models.QuerySet[Lineage]:
    """The edges that consumed exactly this version — `derived_from` spans all of them."""
    return Lineage.objects.filter(source_pgh_id=source.pgh_id).order_by("created", "id")


def source_versions(edges: Iterable[Lineage]) -> list[Version[VersionedModel]]:
    """The source end of each edge, as a version of the model it belongs to."""
    return _resolve_versions([(edge.source_type_id, edge.source_pgh_id) for edge in edges])


def target_versions(edges: Iterable[Lineage]) -> list[Version[VersionedModel]]:
    """The target end of each edge: the versions that were built from something."""
    return _resolve_versions([(edge.target_type_id, edge.target_pgh_id) for edge in edges])


def _resolve_versions(refs: list[tuple[int, uuid.UUID]]) -> list[Version[VersionedModel]]:
    """`(content type of an event table, pgh_id)` pairs as versions, in order and without
    repeats — one source version can feed several versions of the same target.

    A row has a handful of edges, so this reads one at a time rather than grouping by table;
    the grouped form (`_target_object_ids`) is for the graph walk, which does not.
    """
    seen: set[uuid.UUID] = set()
    found = []
    for type_id, pgh_id in refs:
        if pgh_id in seen:
            continue
        seen.add(pgh_id)
        # `get_for_id` is the cached lookup; `edge.source_type` would query per edge.
        event_model = ContentType.objects.get_for_id(type_id).model_class()
        if event_model is None:  # pragma: no cover - a content type without a model
            continue
        row = event_model._base_manager.filter(pgh_id=pgh_id).first()
        if row is None:  # pragma: no cover - event rows are append-only and never deleted
            continue
        found.append(Version(_tracked_model(event_model), as_event_row(row)))
    return found


def _tracked_model(event_model: type[models.Model]) -> type[VersionedModel]:
    """The model an event table mirrors — `Document`, not `DocumentEvent`."""
    tracked = getattr(event_model, "pgh_tracked_model", None)
    if tracked is None:  # pragma: no cover - pghistory sets this on every event model
        raise LineageError(f"{event_model.__name__} does not name the model it tracks")
    return cast("type[VersionedModel]", tracked)


# --- The graph ----------------------------------------------------------------------------------

#: How far `graph()` walks. Deep enough to show a real derivation chain, shallow enough that
#: one page cannot ask the database to walk a tenant's whole history.
MAX_GRAPH_DEPTH = 10
#: And a hard stop on breadth, for the same reason: a drawing of 500 nodes helps nobody.
MAX_GRAPH_NODES = 200


@dataclass(frozen=True)
class Node:
    """One object in a lineage graph — the live row, not a version of it."""

    object_id: uuid.UUID
    model: str
    label: str
    version: int
    deleted: bool
    #: Generation relative to the object the graph was asked about: a source is -1, something
    #: derived from it +1, and a sibling (split off the same parent) comes out level at 0.
    depth: int


@dataclass(frozen=True)
class Edge:
    """`source` (at `source_version`) was used to build `target`."""

    source_id: uuid.UUID
    target_id: uuid.UUID
    source_version: int
    is_stale: bool
    created: datetime


@dataclass(frozen=True)
class Graph:
    root_id: uuid.UUID
    nodes: list[Node]
    edges: list[Edge]


def _target_object_ids(edges: Iterable[Lineage]) -> dict[uuid.UUID, uuid.UUID]:
    """Map each edge's `target_pgh_id` to the object that version belongs to.

    An edge names a *version* of its target; the graph draws objects, so the pointer has to be
    resolved back through the event row. Grouped by content type: one query per target table,
    not one per edge.
    """
    by_type: dict[int, list[uuid.UUID]] = {}
    for edge in edges:
        by_type.setdefault(edge.target_type_id, []).append(edge.target_pgh_id)

    resolved: dict[uuid.UUID, uuid.UUID] = {}
    for type_id, pgh_ids in by_type.items():
        event_model = ContentType.objects.get_for_id(type_id).model_class()
        if event_model is None:  # pragma: no cover - a content type without a model
            continue
        rows = event_model._base_manager.filter(pgh_id__in=pgh_ids).values_list(
            "pgh_id", "pgh_obj_id"
        )
        resolved.update({pgh_id: obj_id for pgh_id, obj_id in rows})
    return resolved


def _describe(object_ids: set[uuid.UUID], model: type[VersionedModel]) -> dict[uuid.UUID, Node]:
    """Live rows for the ids, as graph nodes. Reads soft-deleted rows too — a derived note whose
    source was deleted must still show where it came from."""
    found = {}
    for row in model._base_manager.filter(pk__in=object_ids):
        found[row.pk] = Node(
            object_id=row.pk,
            model=type(row).__name__,
            label=str(row),
            version=row.version,
            deleted=row.deleted_at is not None,
            depth=0,
        )
    return found


def graph(root: VersionedModel, *, depth: int = 3) -> Graph:
    """The piece of the lineage graph around `root`: what it came from, what came from it, and
    what those touch in turn, out to `depth` steps.

    A breadth-first walk over `Lineage` following edges in *both* directions, because the
    interesting shapes are not chains: a sibling (same parent, split off alongside) and a
    co-parent (another source of the same merge) are only reachable by going up and then down
    again. Each step is two indexed queries — which is what the denormalised `source_obj_id`
    on the edge buys.

    `generation` is signed: a parent is one below, a child one above, and a sibling comes out
    level with `root`, which is what makes a layered drawing of it read correctly.
    """
    model = type(root)
    steps = max(0, min(depth, MAX_GRAPH_DEPTH))
    generation: dict[uuid.UUID, int] = {root.pk: 0}
    edges: dict[tuple[uuid.UUID, uuid.UUID], Lineage] = {}
    frontier = {root.pk}

    for _ in range(steps):
        if not frontier or len(generation) >= MAX_GRAPH_NODES:
            break
        rows = {
            edge.pk: edge
            for edge in [
                *Lineage.objects.filter(source_obj_id__in=sorted(frontier)),
                *Lineage.objects.filter(target_pgh_id__in=_version_ids(model, frontier)),
            ]
        }
        targets = _target_object_ids(rows.values())

        discovered: set[uuid.UUID] = set()
        for edge in rows.values():
            target_id = targets.get(edge.target_pgh_id)
            if target_id is None:  # pragma: no cover - unresolvable target version
                continue
            edges.setdefault((edge.source_obj_id, target_id), edge)
            for known, unknown, step in (
                (edge.source_obj_id, target_id, 1),
                (target_id, edge.source_obj_id, -1),
            ):
                if known in generation and unknown not in generation:
                    generation[unknown] = generation[known] + step
                    discovered.add(unknown)
        frontier = discovered

    latest = _latest_versions(model, {edge.source_obj_id for edge in edges.values()})
    described = _describe(set(generation), model)
    nodes = sorted(
        (
            Node(
                object_id=node.object_id,
                model=node.model,
                label=node.label,
                version=node.version,
                deleted=node.deleted,
                depth=generation[object_id],
            )
            for object_id, node in described.items()
        ),
        key=lambda node: (node.depth, node.label),
    )
    return Graph(
        root_id=root.pk,
        nodes=nodes,
        edges=[
            Edge(
                source_id=source_id,
                target_id=target_id,
                source_version=edge.source_version,
                is_stale=edge.source_version < latest.get(source_id, 0),
                created=edge.created,
            )
            for (source_id, target_id), edge in sorted(
                edges.items(), key=lambda item: item[1].created
            )
        ],
    )


def _version_ids(model: type[VersionedModel], object_ids: set[uuid.UUID]) -> list[uuid.UUID]:
    """Every `pgh_id` belonging to these objects — the edge column that names a target."""
    event_model = event_model_for(model)
    if event_model is None:  # pragma: no cover - guarded by the caller
        return []
    rows = event_model._base_manager.filter(**{"pgh_obj_id__in": sorted(object_ids)}).values_list(
        "pgh_id", flat=True
    )
    return [uuid.UUID(str(pgh_id)) for pgh_id in rows]


def _latest_versions(
    model: type[VersionedModel], object_ids: set[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Current version per object, for marking an edge stale."""
    if not object_ids:
        return {}
    rows = model._base_manager.filter(pk__in=sorted(object_ids)).values_list("pk", "version")
    return dict(rows)


def stale_derivations(source: VersionedModel) -> models.QuerySet[Lineage]:
    """Everything derived from a version of `source` that has since been superseded.

    The query the denormalised columns exist for, and the one that makes versioned lineage worth
    the trouble: "what do I have to recompute now that this changed?" is a plain index scan.
    """
    return Lineage.objects.filter(source_obj_id=source.pk, source_version__lt=source.version)
