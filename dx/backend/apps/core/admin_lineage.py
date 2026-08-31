"""The per-object lineage page in the admin: where this object's data came from, and what was
built out of it.

Two tables, deliberately not a graph. The questions people actually arrive with are "which
version of what produced this row" and "what do I have to rebuild now that I changed it", and a
table answers both without anyone having to read a drawing. `apps/core/lineage.py::graph()`
already walks the graph in both directions if a picture is ever wanted.

The downstream table is the valuable one, and it is cheap: an edge stores the denormalised
`(source_obj_id, source_version)`, so "everything derived from this object" is one index scan
and "…from a version that has since been superseded" is the same scan with one more comparison.

Both tables read through `apps.core.admin.scope_to_tenant`, so they follow the same rule as
every other admin page — a superuser with cross-tenant access reads them on the audit alias,
everyone else inside their own tenant context.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.safestring import SafeString

from apps.core.history import EventRow, NotTracked, as_event_row, event_model_for, event_rows
from apps.core.lineage import Lineage

if TYPE_CHECKING:
    from apps.core.admin import BaseModelAdmin
    from apps.core.models import BaseModel


@dataclass(frozen=True)
class UpstreamRow:
    """One source version that fed one version of the object being looked at."""

    target_version: int
    source_label: str
    source_link: SafeString
    source_version: int
    source_is_current: bool
    created: Any


@dataclass(frozen=True)
class DownstreamRow:
    """One thing built out of a version of the object being looked at."""

    target_label: str
    target_link: SafeString
    source_version: int
    is_stale: bool
    created: Any


def _lineage_queryset(request: HttpRequest) -> QuerySet[Lineage]:
    from apps.core.admin import scope_to_tenant  # noqa: PLC0415 - cycle at import time

    return scope_to_tenant(request, Lineage.objects.all())


def _event_queryset(request: HttpRequest, obj: BaseModel) -> QuerySet[Any]:
    from apps.core.admin import scope_to_tenant  # noqa: PLC0415 - cycle at import time

    return scope_to_tenant(request, event_rows(type(obj), obj.pk))


def upstream_rows(request: HttpRequest, obj: BaseModel) -> list[UpstreamRow]:
    """For each version of `obj`, the exact source versions that produced it.

    Keyed on `target_pgh_id`, so a later edit of the object does not re-attribute an earlier
    version to sources it never saw — that is the whole reason an edge names a version rather
    than a row.
    """
    from apps.core.admin import describe_event, event_link  # noqa: PLC0415 - cycle

    versions = list(_event_queryset(request, obj))
    if not versions:
        return []
    by_pgh_id = {as_event_row(row).pgh_id: as_event_row(row) for row in versions}
    edges = _lineage_queryset(request).filter(target_pgh_id__in=list(by_pgh_id)).order_by("created")

    rows = []
    for edge in edges:
        label, source_event = describe_event(
            edge.source_type_id, edge.source_pgh_id, using=edge._state.db
        )
        rows.append(
            UpstreamRow(
                target_version=by_pgh_id[edge.target_pgh_id].version,
                source_label=label,
                source_link=event_link(edge.source_type_id, edge.source_pgh_id, label),
                source_version=edge.source_version,
                # Whether the source has moved on since; the event row is the version that was
                # consumed, so this compares against the source object's version now.
                source_is_current=_source_is_current(request, edge, source_event),
                created=edge.created,
            )
        )
    return sorted(rows, key=lambda row: (-row.target_version, row.source_label))


def _source_is_current(request: HttpRequest, edge: Lineage, source_event: EventRow | None) -> bool:
    """Whether `edge` still points at the source's newest version."""
    if source_event is None:  # pragma: no cover - the version row could not be read
        return False
    latest = _latest_version(request, edge.source_type_id, edge.source_obj_id)
    return latest is not None and edge.source_version >= latest


def _latest_version(request: HttpRequest, event_type_id: int, obj_id: uuid.UUID) -> int | None:
    """The newest version number recorded for one object, read from its event table."""
    from apps.core.admin import scope_to_tenant  # noqa: PLC0415 - cycle

    event_model = ContentType.objects.get_for_id(event_type_id).model_class()
    if event_model is None:  # pragma: no cover - a content type for a removed model
        return None
    queryset = scope_to_tenant(request, event_model._base_manager.all())
    row = queryset.filter(**{"pgh_obj_id": obj_id}).order_by("-version").first()
    return None if row is None else as_event_row(row).version


def downstream_rows(request: HttpRequest, obj: BaseModel) -> list[DownstreamRow]:
    """Everything derived from any version of `obj`, newest first.

    `is_stale` means the consumer was built from a version that has since been superseded —
    the rows someone has to rebuild. Cheap by construction: `source_obj_id`/`source_version`
    are denormalised onto the edge exactly so this is an index scan.
    """
    from apps.core.admin import describe_event, event_link  # noqa: PLC0415 - cycle

    edges = _lineage_queryset(request).filter(source_obj_id=obj.pk).order_by("-created")
    rows = []
    for edge in edges:
        label, _ = describe_event(edge.target_type_id, edge.target_pgh_id, using=edge._state.db)
        rows.append(
            DownstreamRow(
                target_label=label,
                target_link=event_link(edge.target_type_id, edge.target_pgh_id, label),
                source_version=edge.source_version,
                is_stale=edge.source_version < obj.version,
                created=edge.created,
            )
        )
    return rows


def render_lineage_page(
    model_admin: BaseModelAdmin[Any], request: HttpRequest, object_id: str
) -> HttpResponse:
    """The `Lineage` object-tool page hanging off a model's change page."""
    queryset = model_admin.get_queryset(request)
    try:
        obj = queryset.filter(pk=object_id).first()
    except ValueError, TypeError, ValidationError:  # a malformed UUID in the URL
        obj = None
    if obj is None:
        raise Http404("No such object, or it belongs to another tenant.")

    meta = model_admin.model._meta
    tracked = event_model_for(model_admin.model) is not None
    context = {
        **model_admin.admin_site.each_context(request),
        "opts": meta,
        "original": obj,
        "object_id": object_id,
        "title": f"Lineage of {obj}",
        "tracked": tracked,
        "upstream": upstream_rows(request, obj) if tracked else [],
        "downstream": downstream_rows(request, obj) if tracked else [],
        "current_version": obj.version,
        "not_tracked_hint": (
            None
            if tracked
            else f"{meta.verbose_name} is not versioned, so nothing can be derived from it."
        ),
    }
    return render(request, "admin/dx/lineage.html", context)


def object_lineage(request: HttpRequest, obj: BaseModel) -> tuple[list[Any], list[Any]]:
    """(upstream, downstream) for one object — the pair the page renders. Raises `NotTracked`
    when the model has no event table at all."""
    if event_model_for(type(obj)) is None:
        raise NotTracked(f"{type(obj).__name__} is not versioned")
    return upstream_rows(request, obj), downstream_rows(request, obj)
