"""A read-only browser over the data: every model, its newest rows, and for one row the whole
version history and the lineage around it. **Development only.**

Four pages, four hrefs, no JavaScript:

    /explorer/                              every user — pick whose data to look at
    /explorer/<user>/                       every model of every app, with row counts
    /explorer/<user>/jump/?id=…             an id from a log, resolved to whatever holds it
    /explorer/<user>/<app>/<model>/         the rows of one model, newest first
    /explorer/<user>/<app>/<model>/<pk>/    one row *as it is now*: fields, versions, lineage
    /explorer/<user>/<app>/<model>/<pk>/v3/ one *version* of that row: what it held then, what
                                            it was built from, and what was built from it
    /explorer/<user>/edge/<id>/             one lineage edge: the whole call stack that recorded
                                            it, and the build it ran on

Those last two are deliberately different pages, because they are different things and
confusing them is the mistake this whole schema exists to prevent. A row is mutable and has one
current state; a version is immutable and is what a lineage edge actually points at. So every
link out of a lineage table lands on a *version* — the state that was really consumed, not
whatever the row happens to say today — and each page says at the top which of the two it is.

The user comes first because tenant == user: a row belongs to exactly one of them, so "which
tenant" is not a filter on the data, it *is* the root of it. Picking one opens that user's
tenant context for the rest of the page — the same context a request of theirs would run in, so
what the explorer shows is what they would see, not a privileged view over the top of it.

It exists because the interesting thing about this database is not the current row — the admin
and the SPA already show that — but the two structures around it: the version chain
(`apps/core/history.py`) and the derivation graph (`apps/core/lineage.py`). Both are reachable
from the API, one object at a time, if you already know which object to ask about. This is the
page for when you do not: start at the model list and click.

**Not mounted unless `EXPLORER_ENABLED`** (`config/urls.py`; it defaults to `DEBUG`), and every
view checks it again — a dev tool that walks every table has no business being one settings
mistake away from production. It is read-only by construction: no form, no POST, no route that
writes.

Reading another tenant's rows is the point of the tool and the reason it is staff-only and
development-only: it opens `tenant_context(that user)` and lets row-level security decide the
rest, exactly as the admin does for the logged-in user (`AdminTenantMiddleware`). Queries go
through `_base_manager`, which carries neither the ORM scope nor the soft-delete filter, so what
appears is what the *database* is willing to hand over and nothing else — the honest test of
whether the policies work. Soft-deleted rows are included on purpose: an explorer that hid them
would be lying about the one property this schema guarantees.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pghistory.models
from django.apps import apps as django_apps
from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import models
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render, resolve_url
from django.urls import path, reverse
from django.utils import timezone
from django.utils.http import urlencode

from apps.accounts.models import User
from apps.core import lineage, revisions
from apps.core.db import tenant_context
from apps.core.history import Version, as_event_row, event_model_for, versions
from apps.core.models import OwnedModel, VersionedModel
from config.env import BASE_DIR

#: Rows per page. An explorer is for orientation, not for reading a table: the newest few
#: answer "did that write land", and the ones further back are reached by paging or by narrowing
#: the date range rather than by scrolling.
PAGE_SIZE = 20

#: The one-click ranges offered next to the date inputs, as `(key, label)`. Each is an open
#: range ending now — "since", not "between" — which is what a person checking whether a write
#: landed actually wants.
RANGES = (("today", "Today"), ("24h", "24 hours"), ("week", "This week"))

#: The timestamps a table can be filtered on, in the order they are offered. Every row has
#: `created` and `modified` (`VersionedModel`) — except an event row, whose own write time is
#: `pgh_created_at`, and `Lineage`, which is append-only and therefore has no `modified` at all.
TIMESTAMPS = ("pgh_created_at", "created", "modified")

#: How much of a value the pages print before cutting it off. A document's text field would
#: otherwise push the columns that matter off the screen.
MAX_VALUE = 120


# --- what there is to look at -------------------------------------------------------------------


def explorer_models() -> list[type[models.Model]]:
    """Every concrete model this project defines, event tables included, in label order.

    Django's registry rather than a list to maintain: a new app appears here the moment it is
    installed, which is the only way a page like this stays true. Third-party tables
    (`django.contrib.*`, `pghistory_context`) are left out — they are not this app's data, and
    nothing here can say anything useful about them.
    """
    return sorted(
        (
            model
            for model in django_apps.get_models()
            if model._meta.app_config.name.startswith("apps.")
        ),
        key=lambda model: model._meta.label,
    )


def _model_by_name(name: str) -> type[models.Model] | None:
    """A model by its bare class name — `Dataset`, not `datasets.Dataset`.

    Model names are a global namespace in this project (CLAUDE.md "Naming", enforced by
    `test_history.py`), which is what makes this unambiguous: a lineage edge names the class of
    the far end, and that is enough to build a link to it.
    """
    return next((model for model in explorer_models() if model.__name__ == name), None)


def is_event_model(model: type[models.Model]) -> bool:
    return issubclass(model, pghistory.models.Event)


def kind_of(model: type[models.Model]) -> str:
    """Which of the three kinds of table this is — the distinction the whole schema turns on."""
    if is_event_model(model):
        return "history"
    if issubclass(model, OwnedModel):
        return "owned"
    return "shared"


def _order_by(model: type[models.Model]) -> str:
    """Newest first, by the column that means "written" for this kind of table.

    An event table mirrors the tracked row's `created`, so ordering by it would sort versions by
    when the *object* was created — every version of one row landing in a single clump at its
    original position. `pgh_created_at` is when the version itself was written.
    """
    names = {field.name for field in model._meta.fields}
    if "pgh_created_at" in names:
        return "-pgh_created_at"
    if "created" in names:
        return "-created"
    return "-pk"  # UUIDv7: time-ordered anyway (CLAUDE.md "Data model conventions")


def _render(value: object) -> str:
    """One field value as a line of text, short enough to sit in a table cell."""
    if value is None:
        return "—"
    text = str(value)
    return text if len(text) <= MAX_VALUE else f"{text[:MAX_VALUE]}…"


# --- rows for the templates ----------------------------------------------------------------------
#
# Dataclasses rather than model instances or dicts: a template that reaches into an ORM object
# hides its queries, and one that indexes dicts cannot be type-checked at all.


@dataclass(frozen=True)
class UserRow:
    id: str
    username: str
    email: str
    staff: bool
    joined: datetime
    is_you: bool
    url: str


@dataclass(frozen=True)
class ModelRow:
    label: str
    name: str
    kind: str
    tracked: bool
    count: int
    url: str


@dataclass(frozen=True)
class AppGroup:
    label: str
    models: list[ModelRow]


@dataclass(frozen=True)
class ObjectRow:
    pk: str
    label: str
    version: int | None
    #: When this row was written: `pgh_created_at` for a version row, `created` otherwise.
    at: datetime | None
    #: When it last changed. `None` on the tables that have no `modified` — an event row and a
    #: `Lineage` edge are both written once and never updated, so there is nothing to show.
    modified: datetime | None
    deleted: bool
    url: str


@dataclass(frozen=True)
class FieldValue:
    name: str
    value: str


@dataclass(frozen=True)
class RevisionRow:
    """One version inside a save, as the object page lists it."""

    version: int
    model: str
    is_related: bool
    description: str
    deleted: bool
    changes: list[revisions.Change]
    unknown_fields: list[str]
    archived: dict[str, str]
    url: str | None


@dataclass(frozen=True)
class HistoryGroup:
    """Everything one request, task or command wrote, as one revision."""

    source: str
    at: datetime
    revisions: list[RevisionRow]


@dataclass(frozen=True)
class EdgeRow:
    """One lineage edge, as a page shows it: the far end, and how it was recorded."""

    edge_url: str
    model: str
    label: str
    version: int
    is_stale: bool
    at: datetime
    release: str
    frame: str
    code: str
    url: str | None


@dataclass(frozen=True)
class EdgeGroup:
    """The edges one run recorded — the same grouping the revision page uses for versions."""

    source: str
    at: datetime
    edges: list[EdgeRow]


def timestamp_fields(model: type[models.Model]) -> list[str]:
    """Which timestamps this table can be filtered on, most useful first.

    `pgh_created_at` leads where it exists: an event table also mirrors the tracked row's
    `created`, and filtering a version table by when its *object* was created is almost never
    the question being asked.
    """
    names = {field.name for field in model._meta.fields}
    return [name for name in TIMESTAMPS if name in names]


def _day_start(day: date) -> datetime:
    """Midnight local time, as an aware datetime — the columns are `timestamptz`."""
    return timezone.make_aware(datetime.combine(day, time.min))


@dataclass(frozen=True)
class RangeLink:
    """One quick range, and whether it is the one in force."""

    key: str
    label: str
    active: bool


@dataclass(frozen=True)
class DateFilter:
    """The date range the page is showing, and the form that produced it.

    Two ways in, and `range` wins when both are given: a quick range is one click and always
    means "since X, up to now", while the two date inputs are whole days and inclusive at both
    ends — which is what someone typing two dates into a form means. `to` therefore becomes
    `< the next day`, not `<= that day`, which would silently drop everything written after
    midnight on the last day.
    """

    field: str
    fields: list[str]
    start: str
    end: str
    error: str
    #: One of `RANGES`, or "" when the two date inputs are in force.
    preset: str

    @property
    def active(self) -> bool:
        return bool(self.field and (self.preset or self.start or self.end))

    @property
    def ranges(self) -> list[RangeLink]:
        return [RangeLink(key, label, key == self.preset) for key, label in RANGES]

    @property
    def summary(self) -> str:
        """The range in words, for the table's caption."""
        if not self.active:
            return ""
        if self.preset:
            return {"today": "today", "24h": "in the last 24 hours", "week": "this week"}[
                self.preset
            ]
        if self.start and self.end:
            return f"between {self.start} and {self.end}"
        return f"since {self.start}" if self.start else f"up to {self.end}"

    def since(self) -> datetime | None:
        """Where a quick range starts.

        "24 hours" is a rolling window from *now*, not yesterday-and-today: a run at 09:00 asking
        what happened in the last day should not be shown 33 hours of it. "This week" starts on
        Monday, and "today" at local midnight — those two are calendar answers, and a calendar
        answer is what the words mean.
        """
        if self.preset == "24h":
            return timezone.now() - timedelta(hours=24)
        today = timezone.localdate()
        if self.preset == "today":
            return _day_start(today)
        if self.preset == "week":
            return _day_start(today - timedelta(days=today.weekday()))
        return None

    def apply(self, queryset: models.QuerySet[models.Model]) -> models.QuerySet[models.Model]:
        if not self.field:
            return queryset
        if (since := self.since()) is not None:
            return queryset.filter(**{f"{self.field}__gte": since})
        if (start := _parse_day(self.start)) is not None:
            queryset = queryset.filter(**{f"{self.field}__gte": _day_start(start)})
        if (end := _parse_day(self.end)) is not None:
            queryset = queryset.filter(**{f"{self.field}__lt": _day_start(end + timedelta(days=1))})
        return queryset


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def read_date_filter(request: HttpRequest, model: type[models.Model]) -> DateFilter:
    """The filter from the query string, kept in the URL so a filtered page survives a refresh
    and can be pasted to somebody else.

    A malformed date is reported and ignored rather than raising: half a range is still a useful
    answer, and a 500 for a typo in a URL is not.
    """
    fields = timestamp_fields(model)
    requested = request.GET.get("on", "")
    field = requested if requested in fields else (fields[0] if fields else "")
    preset = request.GET.get("range", "").strip()
    if preset not in dict(RANGES):
        preset = ""
    start = "" if preset else request.GET.get("from", "").strip()
    end = "" if preset else request.GET.get("to", "").strip()

    bad = [
        name
        for name, value in (("from", start), ("to", end))
        if value and _parse_day(value) is None
    ]
    error = f"Ignored {' and '.join(bad)}: expected a date like 2026-09-01." if bad else ""
    return DateFilter(field=field, fields=fields, start=start, end=end, error=error, preset=preset)


@dataclass(frozen=True)
class Page:
    """One page of rows, and the links either side of it."""

    number: int
    count: int
    total: int
    previous_url: str | None
    next_url: str | None


def _page_url(request: HttpRequest, number: int) -> str:
    """This page, at another page number — every other query parameter kept.

    The filter lives in the URL, so paging must carry it; losing it on "next" is the classic way
    for a filtered list to lie about what it is showing.
    """
    params = request.GET.copy()
    params["page"] = str(number)
    return f"{request.path}?{params.urlencode()}"


# --- the gate ------------------------------------------------------------------------------------


def _guard(request: HttpRequest) -> HttpResponse | None:
    """The three conditions for answering at all; a response to return, or None to carry on.

    Checked here and not only on the URL: `config/urls.py` decides whether these paths exist,
    this decides whether they answer. Two independent guards, because one edit to a settings
    file must not be able to publish every table.

    Spelled out at the top of each view rather than hidden in a decorator — four views, four
    plain lines, and nobody has to know how a `ParamSpec` threads a URL kwarg to read them.
    """
    if not settings.EXPLORER_ENABLED:
        raise Http404("the explorer is a development tool")
    user = request.user
    if not user.is_authenticated:
        login_url = "admin:login" if settings.ADMIN_ENABLED else settings.LOGIN_URL
        return redirect_to_login(request.get_full_path(), resolve_url(login_url))
    if not user.is_staff:
        # Staff-only for the same reason the admin is: this reads other tenants' rows.
        raise PermissionDenied("the explorer is a staff tool")
    return None


def _tenant_or_404(user_id: uuid.UUID) -> User:
    """The user whose data the rest of the page is about. `User` is a shared table — no scope,
    no policy — so this lookup is the one query that happens outside a tenant context."""
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        raise Http404(f"no user {user_id}")
    return user


def _model_or_404(app_label: str, model_name: str) -> type[models.Model]:
    try:
        model = django_apps.get_model(app_label, model_name)
    except LookupError as exc:
        raise Http404(f"no model {app_label}.{model_name}") from exc
    if model not in explorer_models():
        raise Http404(f"{model._meta.label} is not one of this project's models")
    return model


def object_url(user_id: uuid.UUID, model: type[models.Model], pk: object) -> str:
    """The row as it is now."""
    return reverse(
        "explorer:object", args=[user_id, model._meta.app_label, model._meta.model_name, pk]
    )


def version_url(
    user_id: uuid.UUID, model: type[models.Model], object_id: object, version: int
) -> str:
    """One state of that row. What a lineage edge means, and therefore where one links to."""
    return reverse(
        "explorer:version",
        args=[user_id, model._meta.app_label, model._meta.model_name, object_id, version],
    )


def tracked_model_of(event_model: type[models.Model]) -> type[models.Model] | None:
    """The model an event table mirrors — `ModelA`, not `ModelAEvent`.

    An event row *is* a version, so the explorer never shows one as an object of its own: the
    rows of a history table link to the version page of the row they are a version of.
    """
    tracked: type[models.Model] | None = getattr(event_model, "pgh_tracked_model", None)
    return tracked


# --- the pages -----------------------------------------------------------------------------------


def users(request: HttpRequest) -> HttpResponse:
    """Every user, because every row belongs to one of them. The root of the hierarchy."""
    denied = _guard(request)
    if denied is not None:
        return denied
    return render(
        request,
        "explorer/users.html",
        {
            "jump_url": reverse("explorer:find"),
            "users": [
                UserRow(
                    id=str(user.pk),
                    username=user.get_username(),
                    email=user.email,
                    staff=user.is_staff,
                    joined=user.date_joined,
                    is_you=user.pk == request.user.pk,
                    url=reverse("explorer:index", args=[user.pk]),
                )
                for user in User.objects.order_by("username")
            ],
        },
    )


def index(request: HttpRequest, user_id: uuid.UUID) -> HttpResponse:
    """Every model, grouped by app, with the number of rows this tenant has."""
    denied = _guard(request)
    if denied is not None:
        return denied
    tenant = _tenant_or_404(user_id)

    with tenant_context(tenant.pk):
        groups: dict[str, list[ModelRow]] = {}
        for model in explorer_models():
            meta = model._meta
            groups.setdefault(meta.app_label, []).append(
                ModelRow(
                    label=meta.label,
                    name=model.__name__,
                    kind=kind_of(model),
                    tracked=event_model_for(model) is not None,
                    count=model._base_manager.count(),
                    url=reverse(
                        "explorer:model", args=[tenant.pk, meta.app_label, meta.model_name]
                    ),
                )
            )
        return render(
            request,
            "explorer/index.html",
            {
                "tenant": tenant,
                "jump_url": reverse("explorer:jump", args=[tenant.pk]),
                "apps": [AppGroup(label=label, models=rows) for label, rows in groups.items()],
            },
        )


def model_rows(
    request: HttpRequest, user_id: uuid.UUID, app_label: str, model_name: str
) -> HttpResponse:
    """One page of rows, newest first, soft-deleted ones included, optionally narrowed to a
    date range. Both controls live in the query string, so the page survives a refresh."""
    denied = _guard(request)
    if denied is not None:
        return denied
    tenant = _tenant_or_404(user_id)
    model = _model_or_404(app_label, model_name)

    tracked = tracked_model_of(model) if is_event_model(model) else None

    with tenant_context(tenant.pk):
        date_filter = read_date_filter(request, model)
        queryset = date_filter.apply(model._base_manager.order_by(_order_by(model)))
        paginator = Paginator(queryset, PAGE_SIZE)
        # `get_page`, not `page`: an out-of-range or non-numeric ?page is a clamped page rather
        # than a 404. Nobody types that by hand — it comes from a stale link.
        page = paginator.get_page(request.GET.get("page"))

        rows = [
            ObjectRow(
                pk=str(row.pk),
                label=_render(row),
                version=getattr(row, "version", None),
                at=getattr(row, "pgh_created_at", None) or getattr(row, "created", None),
                modified=getattr(row, "modified", None),
                deleted=getattr(row, "deleted_at", None) is not None,
                url=_row_url(tenant.pk, model, tracked, row),
            )
            for row in page.object_list
        ]
        return render(
            request,
            "explorer/rows.html",
            {
                "tenant": tenant,
                "model": model._meta.label,
                "kind": kind_of(model),
                "versions_of": tracked._meta.label if tracked is not None else "",
                "jump_url": reverse("explorer:jump", args=[tenant.pk]),
                "model_key": model._meta.label_lower,
                "index_url": reverse("explorer:index", args=[tenant.pk]),
                "filter": date_filter,
                "page": Page(
                    number=page.number,
                    count=paginator.num_pages,
                    total=paginator.count,
                    previous_url=(
                        _page_url(request, page.previous_page_number())
                        if page.has_previous()
                        else None
                    ),
                    next_url=(
                        _page_url(request, page.next_page_number()) if page.has_next() else None
                    ),
                ),
                "rows": rows,
            },
        )


def _row_url(
    user_id: uuid.UUID,
    model: type[models.Model],
    tracked: type[models.Model] | None,
    row: models.Model,
) -> str:
    """Where a row in a listing goes: its own page, unless it is an event row, in which case it
    goes to the version page of the object it is a version of."""
    if tracked is None:
        return object_url(user_id, model, row.pk)
    # `as_event_row` is the one place that asserts the shape of a generated event model; the
    # columns below exist only on the subclass, so no type checker can see them otherwise.
    event = as_event_row(row)
    return version_url(user_id, tracked, event.pgh_obj_id, event.version)


def jump(request: HttpRequest, user_id: uuid.UUID | None = None) -> HttpResponse:
    """Resolve an id to the page that holds it, and go there.

    Ids here are UUIDv7 and unique across every table of every tenant (CLAUDE.md "Data model
    conventions"), so an id copied out of a log line or a shell needs no other context to be
    looked up — which is why this is one field and not a field plus two pickers. Off the users
    page there is no tenant yet either, so it searches each one in turn: row-level security means
    a row is only visible from inside its owner's context, and finding it *is* finding out whose
    it is.

    That costs a query per model per tenant in the worst case, which is the honest price of a
    global lookup in a schema with no global index — and it stops at the first hit. `?model=`
    narrows it to one table when the caller already knows (the form on a listing passes it).

    An event row's primary key is its `pgh_id`, so pasting one of those lands on the version it
    identifies rather than on a table nobody wants to read.
    """
    denied = _guard(request)
    if denied is not None:
        return denied

    tenants = [_tenant_or_404(user_id)] if user_id else list(User.objects.order_by("username"))
    fallback = reverse("explorer:index", args=[user_id]) if user_id else reverse("explorer:users")
    back = _safe_back(request.GET.get("back", ""), fallback=fallback)

    wanted = request.GET.get("id", "").strip()
    try:
        key = uuid.UUID(wanted)
    except ValueError:
        return redirect(_with_missing(back, wanted))

    hinted = request.GET.get("model", "")
    candidates = explorer_models()
    if hinted:
        narrowed = [model for model in candidates if model._meta.label_lower == hinted.lower()]
        candidates = narrowed or candidates

    for tenant in tenants:
        with tenant_context(tenant.pk):
            for model in candidates:
                row = model._base_manager.filter(pk=key).first()
                if row is None:
                    continue
                tracked = tracked_model_of(model) if is_event_model(model) else None
                return redirect(_row_url(tenant.pk, model, tracked, row))
    return redirect(_with_missing(back, wanted))


def _with_missing(back: str, wanted: str) -> str:
    """Back where the form was, saying what could not be found — the page renders it."""
    separator = "&" if "?" in back else "?"
    return f"{back}{separator}{urlencode({'missing': wanted})}"


def _safe_back(value: str, *, fallback: str) -> str:
    """Only ever return to a page of this tool. A "where did you come from" parameter that
    accepts anything is an open redirect, dev tool or not."""
    return value if value.startswith("/explorer/") else fallback


@dataclass(frozen=True)
class Frame:
    """One line of a recorded call stack, as the page shows it."""

    depth: int
    location: str
    func: str
    code: str
    #: False for the frames inside site-packages — the same stack, but not this project's code.
    ours: bool


def _frames(stack: list[lineage.StackFrame]) -> list[Frame]:
    """The stack outermost first, with this project's own files told apart from its dependencies.

    Paths are shown relative to the repository where they are inside it. A stack is mostly
    framework plumbing — click, Django's handler, the test client — and the two or three frames
    that belong to the application are the ones anybody reading it is looking for.
    """
    rows = []
    for depth, frame in enumerate(stack, start=1):
        path = Path(frame.file)
        ours = path.is_relative_to(BASE_DIR) and ".venv" not in path.parts
        location = str(path.relative_to(BASE_DIR)) if path.is_relative_to(BASE_DIR) else str(path)
        rows.append(
            Frame(
                depth=depth,
                location=f"{location}:{frame.line}",
                func=frame.func,
                code=frame.code,
                ours=ours,
            )
        )
    return rows


def edge_detail(request: HttpRequest, user_id: uuid.UUID, edge_id: uuid.UUID) -> HttpResponse:
    """One lineage edge: which code claimed the derivation, and on which build.

    The listings only have room for the innermost frame, which answers "where" but not "how did
    we get there" — a derivation recorded from a task, a command and a request looks identical
    at the bottom of the stack and completely different three frames up. `Lineage.stack` is the
    whole thing (`apps/core/lineage.py`), so this page is just it, laid out.
    """
    denied = _guard(request)
    if denied is not None:
        return denied
    tenant = _tenant_or_404(user_id)

    with tenant_context(tenant.pk):
        edge = lineage.Lineage.objects.filter(pk=edge_id).first()
        if edge is None:
            raise Http404(f"no lineage edge {edge_id}")

        source = lineage.source_versions([edge])[0]
        target = lineage.target_versions([edge])[0]
        labels = revisions.context_sources({edge.pgh_context} if edge.pgh_context else set())
        return render(
            request,
            "explorer/edge.html",
            {
                "tenant": tenant,
                "index_url": reverse("explorer:index", args=[tenant.pk]),
                "source": _edge_row(tenant.pk, edge, source),
                "target": _edge_row(tenant.pk, edge, target),
                "at": edge.created,
                "release": edge.release or "unknown",
                "producer": (
                    labels.get(edge.pgh_context, "unknown") if edge.pgh_context else "unknown"
                ),
                "frames": _frames(edge.stack),
            },
        )


def object_detail(
    request: HttpRequest, user_id: uuid.UUID, app_label: str, model_name: str, pk: uuid.UUID
) -> HttpResponse:
    """One row as it is now: its fields, every version of it, and both directions of its
    lineage. The *states* it has been in each have their own page (`object_version`)."""
    denied = _guard(request)
    if denied is not None:
        return denied
    tenant = _tenant_or_404(user_id)
    model = _model_or_404(app_label, model_name)

    with tenant_context(tenant.pk):
        obj = model._base_manager.filter(pk=pk).first()
        if obj is None:
            raise Http404(f"no {model._meta.label} with pk {pk}")

        # An event row is a version, not an object. There is one page for that, and it knows
        # how to show a past state; this one would present the mirrored columns as if they were
        # current, which is precisely the confusion worth designing out.
        tracked = tracked_model_of(model) if is_event_model(model) else None
        if tracked is not None:
            event = as_event_row(obj)
            return redirect(version_url(tenant.pk, tracked, event.pgh_obj_id, event.version))

        return render(
            request,
            "explorer/detail.html",
            {
                "tenant": tenant,
                "model": model._meta.label,
                "kind": kind_of(model),
                "object_id": str(obj.pk),
                "label": _render(obj),
                "version": getattr(obj, "version", None),
                "index_url": reverse("explorer:index", args=[tenant.pk]),
                "model_url": reverse(
                    "explorer:model",
                    args=[tenant.pk, model._meta.app_label, model._meta.model_name],
                ),
                "fields": [
                    FieldValue(name=field.name, value=_render(getattr(obj, field.attname, None)))
                    for field in model._meta.concrete_fields
                ],
                "tracked": event_model_for(model) is not None,
                "history": _history(tenant.pk, obj),
                "sources": _sources(tenant.pk, obj),
                "derived": _derived(tenant.pk, obj),
                "stale_count": _stale_count(obj),
            },
        )


def object_version(
    request: HttpRequest,
    user_id: uuid.UUID,
    app_label: str,
    model_name: str,
    pk: uuid.UUID,
    version: int,
) -> HttpResponse:
    """One state of one row: what it held then, and the lineage recorded against *that* state.

    Not a variant of the object page. The values here are historical, `is_current()` says
    whether they still describe the row, and the lineage tables are the edges of this version
    alone — `sources()` on the object spans every version, which is the wider question.
    """
    denied = _guard(request)
    if denied is not None:
        return denied
    tenant = _tenant_or_404(user_id)
    model = _model_or_404(app_label, model_name)
    if event_model_for(model) is None:
        raise Http404(f"{model._meta.label} is not versioned, so it has no version {version}")

    with tenant_context(tenant.pk):
        chain = versions(model, pk)
        current = next((entry for entry in chain if entry.version == version), None)
        if current is None:
            raise Http404(f"no version {version} of {model._meta.label} {pk}")
        previous = next((entry for entry in chain if entry.version == version - 1), None)

        changes, unknown = revisions.diff(
            model._meta.label, previous.event if previous else None, current.event
        )
        state = current.to_object()
        untracked = current.untracked_fields()
        context_id = current.event.pgh_context_id
        labels = revisions.context_sources({context_id} if context_id else set())

        source_edges = list(lineage.sources_of_version(current.event).order_by("created", "id"))
        target_edges = list(lineage.derived_from_version(current.event))
        return render(
            request,
            "explorer/version.html",
            {
                "tenant": tenant,
                "model": model._meta.label,
                "label": _render(state),
                "object_id": str(pk),
                "version": version,
                "of": len(chain),
                "is_current": current.is_current(),
                "deleted": current.deleted,
                "at": current.at,
                "written_by": labels.get(context_id, "unknown") if context_id else "unknown",
                "event_label": current.event.pgh_label,
                "schema_tag": current.event.pgh_schema,
                "pgh_id": str(current.event.pgh_id),
                "index_url": reverse("explorer:index", args=[tenant.pk]),
                "model_url": reverse(
                    "explorer:model",
                    args=[tenant.pk, model._meta.app_label, model._meta.model_name],
                ),
                "object_page": object_url(tenant.pk, model, pk),
                "previous_url": (
                    version_url(tenant.pk, model, pk, version - 1) if previous else None
                ),
                "next_url": (
                    version_url(tenant.pk, model, pk, version + 1)
                    if any(entry.version == version + 1 for entry in chain)
                    else None
                ),
                "changes": changes,
                "unknown_fields": unknown,
                "fields": [
                    FieldValue(
                        name=field.name,
                        value=(
                            "not tracked at this version"
                            if field.name in untracked
                            else _render(getattr(state, field.attname, None))
                        ),
                    )
                    for field in model._meta.concrete_fields
                ],
                "sources": _group_edges(
                    tenant.pk,
                    _pair(
                        source_edges,
                        lineage.source_versions(source_edges),
                        lambda edge: edge.source_pgh_id,
                    ),
                ),
                "derived": _group_edges(
                    tenant.pk,
                    _pair(
                        target_edges,
                        lineage.target_versions(target_edges),
                        lambda edge: edge.target_pgh_id,
                    ),
                ),
            },
        )


# --- the two structures the page exists for -----------------------------------------------------


def _history(user_id: uuid.UUID, obj: models.Model) -> list[HistoryGroup]:
    """The version chain, folded into the saves that wrote it, each version linked to its page.

    `revisions_of` is the revision page's own data layer, so the explorer shows exactly what the
    API does — diffs, child rows written in the same save, schema-aware. It is typed for owned
    models (the only ones the API exposes); anything else that is tracked still has a plain
    version chain, and one group per version is the honest rendering of it.
    """
    if event_model_for(type(obj)) is None:
        return []
    if isinstance(obj, OwnedModel):
        groups = revisions.group_by_context(revisions.revisions_of(obj))
    elif isinstance(obj, VersionedModel):
        groups = revisions.group_by_context(
            [
                revisions.Revision(
                    pgh_id=version.event.pgh_id,
                    object_id=version.object_id,
                    model=type(obj).__name__,
                    version=version.version,
                    label=version.event.pgh_label,
                    at=version.at,
                    schema_tag=version.event.pgh_schema,
                    schema_known=True,
                    deleted=version.deleted,
                    changes=[],
                    unknown_fields=[],
                    archived={},
                    context_id=version.event.pgh_context_id,
                )
                for version in reversed(obj.history())
            ]
        )
    else:  # pragma: no cover - tracked implies versioned
        return []

    return [
        HistoryGroup(
            source=group.source,
            at=group.at,
            revisions=[
                RevisionRow(
                    version=revision.version,
                    model=revision.model,
                    is_related=revision.is_related,
                    description=revision.description,
                    deleted=revision.deleted,
                    changes=revision.changes,
                    unknown_fields=revision.unknown_fields,
                    archived=revision.archived,
                    url=_revision_url(user_id, revision),
                )
                for revision in group.revisions
            ],
        )
        for group in groups
    ]


def _revision_url(user_id: uuid.UUID, revision: revisions.Revision) -> str | None:
    """The page for that one version. A child row written in the same save belongs to another
    model, which is why this resolves the name rather than assuming the page's own."""
    model = _model_by_name(revision.model)
    if model is None:  # pragma: no cover - every revision names a model of this project
        return None
    return version_url(user_id, model, revision.object_id, revision.version)


def _edge_row(
    user_id: uuid.UUID, edge: lineage.Lineage, far_end: Version[VersionedModel]
) -> EdgeRow:
    """One edge plus the version at its far end. The frame and the release come from the edge
    itself (`Lineage.stack`, `Lineage.release`): which code claimed this derivation, and which
    build it was."""
    frame = edge.stack[-1] if edge.stack else None
    model = far_end.model
    return EdgeRow(
        edge_url=reverse("explorer:edge", args=[user_id, edge.pk]),
        model=model.__name__,
        label=_render(far_end.to_object()),
        version=far_end.version,
        is_stale=not far_end.is_current(),
        at=edge.created,
        release=edge.release or "unknown",
        frame=f"{frame.file.rsplit('/', 1)[-1]}:{frame.line} in {frame.func}()" if frame else "",
        code=frame.code if frame else "",
        # A version, not the object: the edge names the state that was consumed, and sending
        # someone to the live row would answer a question they did not ask.
        url=(
            version_url(user_id, model, far_end.object_id, far_end.version)
            if _model_by_name(model.__name__)
            else None
        ),
    )


def _group_edges(
    user_id: uuid.UUID, pairs: list[tuple[lineage.Lineage, Version[VersionedModel]]]
) -> list[EdgeGroup]:
    """Edges under the run that recorded them.

    An edge takes its `pgh_context` from the version it feeds, so a group is "what this version
    was built from" — and an edge written without a context stands alone rather than being folded
    in with unrelated ones, exactly as `revisions.group_by_context` treats an orphan version.
    """
    groups: dict[object, list[tuple[lineage.Lineage, Version[VersionedModel]]]] = {}
    for index, (edge, far_end) in enumerate(pairs):
        key = edge.pgh_context if edge.pgh_context is not None else ("orphan", index)
        groups.setdefault(key, []).append((edge, far_end))

    labels = revisions.context_sources({edge.pgh_context for edge, _ in pairs if edge.pgh_context})
    return [
        EdgeGroup(
            source=(
                "unknown"
                if members[0][0].pgh_context is None
                else labels.get(members[0][0].pgh_context, "unknown")
            ),
            at=members[0][0].created,
            edges=[_edge_row(user_id, edge, far_end) for edge, far_end in members],
        )
        for members in groups.values()
    ]


def _pair(
    edges: list[lineage.Lineage],
    versions: list[Version[VersionedModel]],
    pgh_id_of: Callable[[lineage.Lineage], uuid.UUID],
) -> list[tuple[lineage.Lineage, Version[VersionedModel]]]:
    """Each edge with the version at its far end.

    Keyed by `pgh_id`, never zipped: `source_versions`/`target_versions` return each version
    *once*, and one source version routinely feeds several versions of the same target — one
    build, then a rebuild after the source moved on. The lists are therefore different lengths
    whenever the graph is interesting.
    """
    by_id = {version.event.pgh_id: version for version in versions}
    return [(edge, by_id[key]) for edge in edges if (key := pgh_id_of(edge)) in by_id]


def _sources(user_id: uuid.UUID, obj: models.Model) -> list[EdgeGroup]:
    """What this row was built from, across every version of it."""
    if not isinstance(obj, VersionedModel) or event_model_for(type(obj)) is None:
        return []
    edges = list(lineage.all_sources_of(obj))
    return _group_edges(
        user_id, _pair(edges, lineage.source_versions(edges), lambda e: e.source_pgh_id)
    )


def _derived(user_id: uuid.UUID, obj: models.Model) -> list[EdgeGroup]:
    """What was built from this row — the same edges, read the other way."""
    if not isinstance(obj, VersionedModel) or event_model_for(type(obj)) is None:
        return []
    edges = list(lineage.derived_from(obj).order_by("created", "id"))
    return _group_edges(
        user_id, _pair(edges, lineage.target_versions(edges), lambda e: e.target_pgh_id)
    )


def _stale_count(obj: models.Model) -> int:
    """Derivations built from a version of this row that has since been superseded — what would
    have to be recomputed now that it changed (`lineage.stale_derivations`)."""
    if not isinstance(obj, VersionedModel) or event_model_for(type(obj)) is None:
        return 0
    return lineage.stale_derivations(obj).count()


urlpatterns = [
    path("", users, name="users"),
    # Same view, with and without a tenant: off the users page it searches every tenant.
    path("jump/", jump, name="find"),
    path("<uuid:user_id>/", index, name="index"),
    path("<uuid:user_id>/jump/", jump, name="jump"),
    path("<uuid:user_id>/edge/<uuid:edge_id>/", edge_detail, name="edge"),
    path("<uuid:user_id>/<str:app_label>/<str:model_name>/", model_rows, name="model"),
    path(
        "<uuid:user_id>/<str:app_label>/<str:model_name>/<uuid:pk>/",
        object_detail,
        name="object",
    ),
    path(
        "<uuid:user_id>/<str:app_label>/<str:model_name>/<uuid:pk>/v<int:version>/",
        object_version,
        name="version",
    ),
]
