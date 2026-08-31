"""Admin bases: soft-delete-safe model pages, read-only history, and tenant scoping.

The admin sees owned data, so it needs the same isolation everything else has — and the two
layers that provide it elsewhere are both absent by default here. `TenantMiddleware` only runs
under `/api/`, so an admin request has no ORM scope (`OwnedManager` raises `ScopeError`) and no
`app.user_id` (row-level security fails closed and every page is empty). `AdminTenantMiddleware`
supplies both from the session user — tenant == user, so a staff user browsing the admin is a
tenant browsing their own rows, and the database enforces that, not this module.

`scope_to_tenant()` is the one place that decides which rows an admin page may read. Every
`get_queryset` here goes through it, and `apps/core/tests/test_admin.py` asserts that they do:
one function is something a leak test can actually verify, a filter repeated in six overrides
is not.

## Cross-tenant access for superusers

A superuser sees every tenant, but not by loosening a policy: the queryset is routed to a
second database alias (`settings.AUDIT_DB_ALIAS`) that connects as `app_admin` (BYPASSRLS), the
same role `manage.py shell_admin` uses. The default connection stays `app_user`, so the
readiness probe keeps refusing a web process that could bypass the policies on its own
connection, and an ordinary request cannot reach the alias — only the admin code below names it.

The alias exists only when `DB_ADMIN_*` is configured (`Env.audit_credentials`). Unset is the
production default and a supported state: a superuser then sees their own tenant like anybody
else. Every cross-tenant page view is logged to `tenant.admin_access`, as `shell_admin` is.

Writes stay inside the acting user's own tenant. Cross-tenant mode is read-only — not because
the role could not write, but because an edit made while looking at all tenants at once is
almost never the intended one, and `owner` is `editable=False`, so the admin has no way to say
which tenant a new row would belong to.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, TypeVar, cast

import structlog
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.options import Action, ActionLocation
from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, models, transaction
from django.db.models import ForeignKey, QuerySet
from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.urls import URLPattern, path, reverse
from django.utils.html import format_html
from django.utils.safestring import SafeString
from django_scopes import scopes_disabled
from pghistory.admin import EventModelAdmin, EventsAdmin

from apps.core.db import NoTenantContext, tenant_context
from apps.core.history import EventRow, as_event_row, event_model_for
from apps.core.lineage import Lineage
from apps.core.models import BaseModel, OwnedModel

if TYPE_CHECKING:
    from django.forms import ModelChoiceField

    from apps.accounts.models import User

audit = structlog.get_logger("tenant.admin_access")

_ModelT = TypeVar("_ModelT", bound=models.Model)

#: Set on the request by `AdminTenantMiddleware` when the page is being served with
#: cross-tenant access. An attribute rather than a helper reading `request.user` again: the
#: middleware decides once, and everything downstream reads that one decision.
CROSS_TENANT_ATTR = "admin_cross_tenant"


def audit_alias() -> str | None:
    """The cross-tenant database alias, or None when it is not configured."""
    alias: str = settings.AUDIT_DB_ALIAS
    return alias if alias in settings.DATABASES else None


def may_cross_tenants(user: User | AnonymousUser) -> bool:
    """Whether this user gets every tenant's rows: superusers, once the alias is deployed."""
    return bool(audit_alias() is not None and user.is_active and user.is_superuser)


def cross_tenant_active(request: HttpRequest) -> bool:
    """Whether *this request* is being served across tenants (set by the middleware)."""
    return bool(getattr(request, CROSS_TENANT_ATTR, False))


def scope_to_tenant[ModelT: models.Model](
    request: HttpRequest, queryset: QuerySet[ModelT]
) -> QuerySet[ModelT]:
    """Restrict an admin queryset to the rows this request may see. The only such decision.

    In the normal case it changes nothing and does not have to: the request runs inside
    `tenant_context(request.user.pk)`, so the ORM scope filters the queryset and the row-level
    security policy filters the query — the same two layers the API gets. For a superuser with
    cross-tenant access it moves the queryset onto the audit alias, whose role the policies do
    not bind.
    """
    if cross_tenant_active(request):
        alias = audit_alias()
        if alias is not None:  # pragma: no branch - the middleware checked the same thing
            return queryset.using(alias)
    return queryset


@contextmanager
def admin_tenant_context(request: HttpRequest) -> Iterator[None]:
    """The tenant context an admin request runs in.

    Cross-tenant requests get no context at all and no ORM scope: the audit alias ignores the
    database variable anyway, and leaving the default connection without one keeps it failing
    closed, so a queryset that forgets `scope_to_tenant` returns nothing instead of the acting
    user's rows dressed up as everyone's.
    """
    user = request.user
    if cross_tenant_active(request):
        audit.warning(
            "admin_cross_tenant_access",
            username=user.get_username(),
            path=request.path,
            method=request.method or "",
        )
        with scopes_disabled():
            yield
        return
    user_id = user.pk
    if not isinstance(user_id, uuid.UUID):  # pragma: no cover - the caller checked is_authenticated
        raise NoTenantContext(f"admin request has no usable user id: {user_id!r}")
    with tenant_context(user_id):
        yield


class AdminTenantMiddleware:
    """Runs `/admin/` requests inside a tenant context (see the module docstring).

    Separate from `TenantMiddleware` because the two resolve different identities: that one
    trusts a bearer token and nothing else, this one trusts the admin session. Keeping them
    apart is what stops a session cookie from ever authenticating the API.

    Must come after `AuthenticationMiddleware` (it reads `request.user`) and after
    `HistoryMiddleware`, so an admin write lands in a context like any other.
    """

    prefix = "/admin/"

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        if not request.path.startswith(self.prefix):
            return self.get_response(request)
        user = request.user
        if not user.is_authenticated:  # the login page, which reads no owned data
            return self.get_response(request)

        setattr(request, CROSS_TENANT_ATTR, may_cross_tenants(user))
        with admin_tenant_context(request):
            response = self.get_response(request)
        if response.streaming and not isinstance(response, HttpResponse):
            # Same trap as the API's: the transaction is over before the body is produced.
            return response
        return response


class BaseModelAdmin[ModelT: BaseModel](admin.ModelAdmin[ModelT]):
    """Base for every `BaseModel` subclass registered in the admin.

    Soft-delete aware and hard-delete safe: the delete button and `delete_selected` both issue a
    real `DELETE`, which the `no_hard_delete` trigger rejects — the admin would surface a raw
    Postgres error as a 500. Refusing at the permission layer is the same answer given earlier
    and in a form the user can read.
    """

    list_filter = ["deleted_at"]
    actions = ["soft_delete_action", "restore_action"]
    #: Database-owned columns (apps/core/models.py::BaseModel). Never editable, always worth
    #: seeing — `version` is what the history pages order by.
    readonly_fields = ["id", "created", "modified", "version", "deleted_at"]

    def has_delete_permission(self, request: HttpRequest, obj: ModelT | None = None) -> bool:
        """Nothing is ever hard-deleted (CLAUDE.md "Versioning, history and lineage").
        `soft_delete_action` is the delete this project has; erasure is `manage.py
        delete_tenant`."""
        return False

    def has_add_permission(self, request: HttpRequest) -> bool:
        # `owner` is editable=False and comes from the tenant context, so a cross-tenant page —
        # which deliberately has no context — cannot say whose the new row would be.
        return not cross_tenant_active(request)

    def has_change_permission(self, request: HttpRequest, obj: ModelT | None = None) -> bool:
        return not cross_tenant_active(request)

    def get_actions(
        self,
        request: HttpRequest,
        action_location: ActionLocation = ActionLocation.CHANGE_LIST,
    ) -> dict[str, Action | None]:
        """`delete_selected` issues a real `DELETE`, which the database rejects — drop it here
        as well as refusing the permission, so it is not merely greyed out."""
        actions = super().get_actions(request, action_location=action_location)
        actions.pop("delete_selected", None)
        if cross_tenant_active(request):
            return {}
        return actions

    def get_queryset(self, request: HttpRequest) -> QuerySet[ModelT]:
        """`all_objects`, not `objects`: soft-deleted rows have to stay visible here, because
        this is the only interface that can restore them."""
        manager = getattr(self.model, "all_objects", self.model._default_manager)
        return scope_to_tenant(request, manager.get_queryset())

    def formfield_for_foreignkey(
        self,
        db_field: ForeignKey[Any, Any],
        request: HttpRequest,
        **kwargs: Any,  # noqa: ANN401 - Django's widget kwargs, passed straight through
    ) -> ModelChoiceField[Any] | None:
        """Let a form keep an FK that points at a soft-deleted row.

        Admin choice fields query `_default_manager`, which hides them — so re-saving a
        `Dataset` whose `Tag` was soft-deleted fails validation with "Select a valid choice"
        even when nobody touched that field.
        """
        related = db_field.remote_field.model
        if issubclass(related, BaseModel):
            manager = getattr(related, "all_objects", related._default_manager)
            kwargs["queryset"] = scope_to_tenant(request, manager.get_queryset())
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @admin.action(description="Soft delete selected")
    def soft_delete_action(self, request: HttpRequest, queryset: QuerySet[ModelT]) -> None:
        """One `soft_delete()` per row, not a bulk `.update()`: a service may have cascade
        rules of its own, and each row must get its own version."""
        done = 0
        for obj in queryset.filter(deleted_at__isnull=True):
            obj.soft_delete()
            done += 1
        self.message_user(request, f"Soft-deleted {done} row(s).", messages.SUCCESS)

    @admin.action(description="Restore selected")
    def restore_action(self, request: HttpRequest, queryset: QuerySet[ModelT]) -> None:
        """Undo a soft delete, one row at a time.

        Every unique constraint on a `BaseModel` is conditioned on `deleted_at__isnull=True`,
        so a name freed by a delete can be taken again — and then the restore collides. That is
        a normal outcome, not a bug: report which row and carry on with the rest.
        """
        restored, clashed = 0, []
        for obj in queryset.filter(deleted_at__isnull=False):
            obj.deleted_at = None
            try:
                # Its own savepoint: an IntegrityError marks the transaction for rollback, and
                # without this the next row's save would fail with "current transaction is
                # aborted" instead of its own answer.
                with transaction.atomic():
                    obj.save(update_fields=["deleted_at"])
            except IntegrityError:
                clashed.append(str(obj))
            else:
                restored += 1
        if restored:
            self.message_user(request, f"Restored {restored} row(s).", messages.SUCCESS)
        if clashed:
            self.message_user(
                request,
                f"Could not restore {len(clashed)} row(s) — a live row already uses that value: "
                + ", ".join(clashed),
                messages.WARNING,
            )


class OwnedModelAdmin[ModelT: OwnedModel](BaseModelAdmin[ModelT]):
    """`BaseModelAdmin` plus the lineage page every owned object gets (see `lineage_view`)."""

    change_form_template = "admin/dx/change_form_lineage.html"

    def get_urls(self) -> list[URLPattern]:
        meta = self.model._meta
        view = self.admin_site.admin_view(self.lineage_view)
        return [
            path(
                "<path:object_id>/lineage/",
                view,
                name=f"{meta.app_label}_{meta.model_name}_lineage",
            ),
            *super().get_urls(),
        ]

    def lineage_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        """Where one object's data came from, and what was built out of it."""
        from apps.core.admin_lineage import render_lineage_page  # noqa: PLC0415 - cycle

        return render_lineage_page(self, request, object_id)


class ReadOnlyEventAdmin(EventModelAdmin):
    """Base for every event model's admin page.

    Event tables are append-only in the database (`PGHISTORY_APPEND_ONLY`), so an edit here
    surfaces as a trigger error; refuse at the permission layer instead. Subclasses
    `pghistory.admin.EventModelAdmin`, not a plain `ModelAdmin`, so the cross-links between a
    tracked object and its events keep working.
    """

    list_display = ["pgh_created_at", "pgh_label", "pgh_obj_id", "version", "pgh_schema"]
    list_filter = ["pgh_label", "pgh_schema"]

    def has_add_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return False

    def get_queryset(self, request: HttpRequest) -> QuerySet[models.Model]:
        return scope_to_tenant(request, super().get_queryset(request))


class TenantEventsAdmin(EventsAdmin):
    """The aggregate events page (`PGHISTORY_ADMIN_CLASS`), scoped.

    The page unions every event table and exposes only the columns that union has in common, so
    it has no tenant column of its own to filter on — the stage's "custom proxy carrying
    `owner_id`" is not available: pghistory only lets a proxy field read `pgh_context`, and the
    context table is deliberately free of identifiers.

    It does not need one. Every event table here mirrors an `OwnedModel`, so each carries a real
    `owner_id` column with the `tenant_isolation` policy on it, and the union is assembled from
    those tables: without a tenant context the page returns nothing rather than everything.
    `.references(user)` adds the ORM-layer half of the usual pair, filtering the same real
    column the policy keys on.
    """

    def get_queryset(self, request: HttpRequest) -> QuerySet[models.Model]:
        # pghistory ships no type hints for its admin, hence the cast at this one edge.
        inherited = cast("QuerySet[models.Model]", super().get_queryset(request))  # type: ignore[no-untyped-call]
        queryset = scope_to_tenant(request, inherited)
        if not cross_tenant_active(request) and request.user.is_authenticated:
            # EventsQuerySet only; `references` filters each unioned table on the foreign keys
            # pointing at this user, which for an owned event table is `owner_id`.
            references = getattr(queryset, "references", None)
            if references is not None:  # pragma: no branch - always an EventsQuerySet
                queryset = references(request.user)
        return queryset


# --- Lineage ----------------------------------------------------------------------------------


def event_admin_url(content_type_id: int, pgh_id: uuid.UUID) -> str | None:
    """Admin change-page URL for one event row, or None if it no longer resolves.

    It should always resolve — event rows are append-only and nothing hard-deletes them — but a
    lineage page that raises is exactly the page you wanted when that stops being true.
    """
    from django.contrib.contenttypes.models import ContentType  # noqa: PLC0415 - app registry

    try:
        event_model = ContentType.objects.get_for_id(content_type_id).model_class()
    except ContentType.DoesNotExist:  # pragma: no cover - a content type for a removed model
        return None
    if event_model is None:  # pragma: no cover - ditto
        return None
    meta = event_model._meta
    try:
        return reverse(f"admin:{meta.app_label}_{meta.model_name}_change", args=[pgh_id])
    except Exception:  # noqa: BLE001 - the event model may not be registered
        return None


def event_link(content_type_id: int, pgh_id: uuid.UUID, label: str) -> SafeString:
    """`label` as a link to that event row's admin page, or plain text if it does not resolve."""
    url = event_admin_url(content_type_id, pgh_id)
    if url is None:
        return format_html("<span title='no longer resolves'>{} (missing)</span>", label)
    return format_html('<a href="{}">{}</a>', url, label)


def describe_event(
    content_type_id: int, pgh_id: uuid.UUID, *, using: str | None = None
) -> tuple[str, EventRow | None]:
    """A short human label for an event row, and the row itself when it could be read.

    `using` is the alias the *edge* was read on (`obj._state.db`), not a choice made here: on a
    cross-tenant page the edge came off the audit alias, and looking its source up on the default
    connection would find nothing and render every row as "missing".
    """
    from django.contrib.contenttypes.models import ContentType  # noqa: PLC0415 - app registry

    event_model = ContentType.objects.get_for_id(content_type_id).model_class()
    if event_model is None:  # pragma: no cover - a content type for a removed model
        return "(unknown)", None
    tracked_model = getattr(event_model, "pgh_tracked_model", None)
    name = tracked_model.__name__ if tracked_model is not None else event_model.__name__
    queryset = event_model._base_manager.all()
    if using is not None:
        queryset = queryset.using(using)
    row = queryset.filter(pgh_id=pgh_id).first()
    if row is None:
        return f"{name} (missing version)", None
    event = as_event_row(row)
    return f"{name} v{event.version}", event


@admin.register(Lineage)
class LineageAdmin(admin.ModelAdmin[Lineage]):
    """The derivation graph, read-only.

    Not a `BaseModelAdmin`: `Lineage` is not a `BaseModel`. It has no version chain and no soft
    delete — it is append-only, enforced by its own triggers.
    """

    list_display = ["created", "target_link", "source_link", "source_version", "pgh_context"]
    list_filter = ["source_type", "target_type"]
    search_fields = ["source_obj_id"]
    ordering = ["-created"]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Lineage | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Lineage | None = None) -> bool:
        return False

    def get_queryset(self, request: HttpRequest) -> QuerySet[Lineage]:
        return scope_to_tenant(request, super().get_queryset(request))

    @admin.display(description="Target")
    def target_link(self, obj: Lineage) -> SafeString:
        label, _ = describe_event(obj.target_type_id, obj.target_pgh_id, using=obj._state.db)
        return event_link(obj.target_type_id, obj.target_pgh_id, label)

    @admin.display(description="Source")
    def source_link(self, obj: Lineage) -> SafeString:
        label, _ = describe_event(obj.source_type_id, obj.source_pgh_id, using=obj._state.db)
        return event_link(obj.source_type_id, obj.source_pgh_id, label)


def register_event_admins() -> None:
    """Register a `ReadOnlyEventAdmin` for the event model of every registered tracked model.

    Called from `apps.core.apps.CoreConfig.ready()` after the feature apps have registered their
    own models, so "which histories are browsable" follows from which models the admin shows
    rather than from a second list that can drift from it.
    """
    for model in list(admin.site._registry):
        event_model = event_model_for(model)
        if event_model is not None and event_model not in admin.site._registry:
            admin.site.register(event_model, ReadOnlyEventAdmin)


__all__ = [
    "AdminTenantMiddleware",
    "BaseModelAdmin",
    "LineageAdmin",
    "OwnedModelAdmin",
    "ReadOnlyEventAdmin",
    "TenantEventsAdmin",
    "cross_tenant_active",
    "may_cross_tenants",
    "register_event_admins",
    "scope_to_tenant",
]
