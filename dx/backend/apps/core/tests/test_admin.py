"""The admin's safety contract: no hard deletes, no editable history, no cross-tenant leaks.

The two leak tests are the security boundary of this feature — everything else here is
regression protection. They are written against the rendered changelist rather than a queryset
on purpose: a filter that is right in `get_queryset` and forgotten in a `list_filter` lookup
still leaks, and it is the page a person looks at.

Cross-tenant tests need `transaction=True`. The audit alias is a second database session, so it
cannot see rows a normal test wrote inside its (uncommitted) transaction — which is exactly the
property that makes it able to see other tenants at all.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from django.conf import settings
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.http import HttpRequest
from django.test import Client, RequestFactory
from django.urls import resolve, reverse
from pghistory.models import Events

from apps.accounts.models import User
from apps.core import history
from apps.core.admin import (
    BaseModelAdmin,
    LineageAdmin,
    ReadOnlyEventAdmin,
    TenantEventsAdmin,
    scope_to_tenant,
)
from apps.core.lineage import Lineage
from apps.core.models import BaseModel
from apps.core.testing import acting_as
from apps.datasets.api import create_dataset_for, set_dataset_tags
from apps.datasets.models import Dataset, DatasetTag, Tag

# --- fixtures ---------------------------------------------------------------------------------


def _staff(username: str, *, superuser: bool = False) -> User:
    user = User.objects.create_user(username, f"{username}@example.com", "pw", is_staff=True)
    if superuser:
        user.is_superuser = True
        user.save(update_fields=["is_superuser"])
    else:
        # A real staff account, not a superuser shortcut: `has_change_permission` and the
        # changelist both go through the permission machinery.
        user.user_permissions.set(Permission.objects.all())
    return user


@pytest.fixture
def staff(db: None) -> User:
    return _staff("editor")


@pytest.fixture
def admin_client_for() -> Callable[[User], Client]:
    def make(user: User) -> Client:
        client = Client()
        client.force_login(user)
        return client

    return make


class _CollectingMessages:
    """`message_user` needs a message store; the real one needs a session on the request."""

    def __init__(self) -> None:
        self.collected: list[str] = []

    def add(self, level: int, message: str, extra_tags: str = "") -> None:
        self.collected.append(str(message))


def _request(user: User, *, cross_tenant: bool = False) -> HttpRequest:
    """A request as the admin views see it: what `AdminTenantMiddleware` puts on it, plus the
    two things `ModelAdmin` reads off a real request — the resolver match and a message store."""
    from apps.core.admin import CROSS_TENANT_ATTR

    request = RequestFactory().get("/admin/")
    request.user = user
    request.resolver_match = resolve("/admin/")
    request._messages = _CollectingMessages()  # type: ignore[attr-defined]
    setattr(request, CROSS_TENANT_ATTR, cross_tenant)
    return request


def _model_admins() -> list[tuple[type[BaseModel], admin.ModelAdmin[BaseModel]]]:
    return [
        (model, model_admin)
        for model, model_admin in admin.site._registry.items()
        if issubclass(model, BaseModel)
    ]


# --- Task 2: nothing in the admin can hard-delete ---------------------------------------------


def test_no_admin_can_hard_delete(staff: User) -> None:
    """Every registered `BaseModel` admin refuses delete. The database refuses it too (the
    `no_hard_delete` trigger), but as a 500 with a raw Postgres error."""
    request = _request(staff)
    for model, model_admin in _model_admins():
        assert not model_admin.has_delete_permission(request), model._meta.label
        assert "delete_selected" not in model_admin.get_actions(request), model._meta.label


def test_base_model_admins_use_base_class() -> None:
    """A plain `ModelAdmin` reintroduces the delete button and reads through `objects`, which
    hides soft-deleted rows from the only interface that can restore them."""
    for model, model_admin in _model_admins():
        assert isinstance(model_admin, BaseModelAdmin), (
            f"{model._meta.label} uses {type(model_admin).__name__}, not BaseModelAdmin"
        )


def test_base_model_admins_read_soft_deleted_rows(staff: User) -> None:
    """`objects` would filter them out, and the admin is the only interface that can restore
    them — so the changelist query must carry no `deleted_at IS NULL`."""
    with acting_as(staff):
        for model, model_admin in _model_admins():
            sql = str(model_admin.get_queryset(_request(staff)).query)
            # The column is of course selected; what must not be there is the filter on it.
            assert '"deleted_at" IS NULL' not in sql, (
                f"{model._meta.label} hides soft-deleted rows from the only page that can "
                "restore them"
            )


def test_soft_delete_action_produces_a_version_row(staff: User) -> None:
    with acting_as(staff):
        dataset = create_dataset_for(staff, name="doomed", description="")
    model_admin = admin.site._registry[Dataset]
    request = _request(staff)

    with acting_as(staff):
        model_admin.soft_delete_action(request, Dataset.all_objects.filter(pk=dataset.pk))
        dataset.refresh_from_db()
        versions = [history.as_event_row(row) for row in history.event_rows(Dataset, dataset.pk)]

    assert dataset.deleted_at is not None
    assert dataset.version == 2  # the soft delete is an UPDATE like any other
    assert [row.version for row in versions] == [2, 1]
    assert versions[0].deleted_at is not None


def test_restore_action_reports_a_unique_collision_instead_of_raising(staff: User) -> None:
    """Unique constraints are conditioned on `deleted_at__isnull=True`, so a name freed by a
    delete can be taken again — and then the restore cannot happen. That is a message, not a
    500."""
    with acting_as(staff):
        first = create_dataset_for(staff, name="ds", description="", tags=["sales"])
        set_dataset_tags(staff, first, [])  # retires the link and, with it, the orphaned tag
        first.soft_delete()
        # Same tag name again: allowed, because the old one is soft-deleted.
        create_dataset_for(staff, name="ds2", description="", tags=["sales"])
        retired = Tag.all_objects.filter(name="sales", deleted_at__isnull=False)
        assert retired.exists()

        model_admin = admin.site._registry[Tag]
        request = _request(staff)
        model_admin.restore_action(request, retired)

    assert any("Could not restore" in message for message in request._messages.collected)  # type: ignore[attr-defined]
    with acting_as(staff):
        assert Tag.all_objects.filter(name="sales", deleted_at__isnull=False).exists()


@pytest.mark.django_db
def test_admin_can_save_object_with_soft_deleted_fk(
    staff: User, admin_client_for: Callable[[User], Client]
) -> None:
    """Admin choice fields query `_default_manager`, which hides soft-deleted rows: re-saving a
    row whose FK target was deleted would fail with "Select a valid choice" even though nobody
    touched that field."""
    with acting_as(staff):
        dataset = create_dataset_for(staff, name="ds", description="", tags=["sales"])
        link = DatasetTag.all_objects.get(dataset=dataset)
        tag = link.tag
        tag.soft_delete()

    client = admin_client_for(staff)
    url = reverse("admin:datasets_datasettag_change", args=[link.pk])
    response = client.post(url, {"dataset": str(dataset.pk), "tag": str(tag.pk)}, follow=True)

    assert response.status_code == 200
    assert "Select a valid choice" not in response.content.decode()
    with acting_as(staff):
        assert DatasetTag.all_objects.filter(pk=link.pk).exists()


# --- Task 3: event admins are read-only -------------------------------------------------------


def test_event_admins_are_read_only(staff: User) -> None:
    request = _request(staff)
    event_admins = [
        (model, model_admin)
        for model, model_admin in admin.site._registry.items()
        if isinstance(model_admin, ReadOnlyEventAdmin)
    ]
    assert event_admins, "no event model is registered in the admin"
    for model, model_admin in event_admins:
        assert not model_admin.has_add_permission(request), model._meta.label
        assert not model_admin.has_change_permission(request), model._meta.label
        assert not model_admin.has_delete_permission(request), model._meta.label


def test_every_tracked_registered_model_has_an_event_admin() -> None:
    """Registering a model in the admin brings its history along; nothing keeps a second list."""
    for model, _ in _model_admins():
        event_model = history.event_model_for(model)
        if event_model is not None:
            assert event_model in admin.site._registry, model._meta.label


def test_event_admin_shows_the_schema_tag() -> None:
    """A version row written under an older tag must not present a backfilled default as data;
    the tag is what says so."""
    for model_admin in admin.site._registry.values():
        if isinstance(model_admin, ReadOnlyEventAdmin):
            assert "pgh_schema" in model_admin.list_display


# --- Task 4/7: the leak tests — the security boundary -----------------------------------------


@pytest.mark.django_db
def test_events_admin_does_not_leak_across_tenants(
    staff: User, other_user: User, admin_client_for: Callable[[User], Client]
) -> None:
    """Events written by two tenants; the aggregate page of one shows only that one's."""
    with acting_as(staff):
        mine = create_dataset_for(staff, name="alice-dataset", description="")
    with acting_as(other_user):
        theirs = create_dataset_for(other_user, name="bob-dataset", description="")
        theirs.description = "edited, so this version carries a diff to render"
        theirs.save()

    client = admin_client_for(staff)
    url = reverse("admin:pghistory_events_changelist")
    body = client.get(url, {"event_model": "datasets.datasetevent"}).content.decode()

    assert str(mine.pk) in body, "the page shows nothing at all — the filter did not take"
    assert str(theirs.pk) not in body
    assert "bob-dataset" not in body


@pytest.mark.django_db
def test_events_admin_queryset_is_scoped(staff: User, other_user: User) -> None:
    """The same guarantee at the queryset level, independent of how the page renders."""
    with acting_as(staff):
        create_dataset_for(staff, name="alice-dataset", description="")
    with acting_as(other_user):
        create_dataset_for(other_user, name="bob-dataset", description="")

    model_admin = admin.site._registry[Events]
    assert isinstance(model_admin, TenantEventsAdmin)
    with acting_as(staff):
        names = {row.pgh_data.get("name") for row in model_admin.get_queryset(_request(staff))}

    assert "alice-dataset" in names
    assert "bob-dataset" not in names


@pytest.mark.django_db
def test_lineage_admin_does_not_leak_across_tenants(
    staff: User, other_user: User, admin_client_for: Callable[[User], Client]
) -> None:
    with acting_as(staff):
        _record_edge(staff, "alice-source")
    with acting_as(other_user):
        _record_edge(other_user, "bob-source")

    client = admin_client_for(staff)
    body = client.get(reverse("admin:core_lineage_changelist")).content.decode()

    with acting_as(staff):
        mine = list(Lineage.objects.values_list("id", flat=True))
    with acting_as(other_user):
        theirs = list(Lineage.objects.values_list("id", flat=True))

    assert mine and theirs
    assert all(str(edge_id) not in body for edge_id in theirs)


def _record_edge(user: User, name: str) -> Lineage:
    """One derivation edge owned by `user`: a dataset derived from another dataset."""
    from apps.core.lineage import record_derivation

    source = create_dataset_for(user, name=name, description="")
    target = create_dataset_for(user, name=f"{name}-derived", description="")
    return record_derivation(target, sources=[source])[0]


def test_admin_querysets_all_go_through_scope_to_tenant() -> None:
    """One helper, called everywhere — that is what makes the leak tests above meaningful.

    Checked at the source level: an override that filters by hand would pass the two leak tests
    today and drift apart from them at the next change.
    """
    import inspect

    for model, model_admin in admin.site._registry.items():
        if model._meta.app_label in {"auth", "accounts"}:
            continue  # shared tables: no owner column, no tenant to scope to
        get_queryset = type(model_admin).get_queryset
        source = inspect.getsource(get_queryset)
        assert "scope_to_tenant" in source, (
            f"{type(model_admin).__name__}.get_queryset does not go through scope_to_tenant"
        )


def test_scope_to_tenant_is_a_noop_without_cross_tenant_access(staff: User) -> None:
    """In the normal case the isolation is the request's tenant context plus RLS, not a filter
    bolted on here — so the helper must not quietly add a second, weaker one."""
    with acting_as(staff):
        queryset = Dataset.all_objects.all()
        assert scope_to_tenant(_request(staff), queryset).db == queryset.db


# --- Cross-tenant superuser access ------------------------------------------------------------


@pytest.mark.django_db(transaction=True, databases=["default", "audit"])
def test_superuser_sees_every_tenant(admin_client_for: Callable[[User], Client]) -> None:
    """The one page in the app that crosses tenants, and only for a superuser with the alias
    deployed."""
    root = _staff("root", superuser=True)
    alice = User.objects.create_user("alice", "alice@example.com", "pw")
    bob = User.objects.create_user("bob", "bob@example.com", "pw")
    with acting_as(alice):
        create_dataset_for(alice, name="alice-dataset", description="")
    with acting_as(bob):
        create_dataset_for(bob, name="bob-dataset", description="")

    body = admin_client_for(root).get(reverse("admin:datasets_dataset_changelist")).content.decode()

    assert "alice-dataset" in body
    assert "bob-dataset" in body


@pytest.mark.django_db(transaction=True, databases=["default", "audit"])
def test_staff_user_never_sees_another_tenant(
    admin_client_for: Callable[[User], Client],
) -> None:
    """Same page, non-superuser: their own tenant only, even with the alias deployed."""
    editor = _staff("editor")
    bob = User.objects.create_user("bob", "bob@example.com", "pw")
    with acting_as(editor):
        create_dataset_for(editor, name="editor-dataset", description="")
    with acting_as(bob):
        create_dataset_for(bob, name="bob-dataset", description="")

    body = (
        admin_client_for(editor).get(reverse("admin:datasets_dataset_changelist")).content.decode()
    )

    assert "editor-dataset" in body
    assert "bob-dataset" not in body


@pytest.mark.django_db(transaction=True, databases=["default", "audit"])
def test_cross_tenant_pages_are_read_only(admin_client_for: Callable[[User], Client]) -> None:
    """Looking at every tenant at once is not the moment to edit one of them, and `owner` is
    editable=False, so a cross-tenant page could not say whose a new row would be."""
    root = _staff("root", superuser=True)
    alice = User.objects.create_user("alice", "alice@example.com", "pw")
    with acting_as(alice):
        dataset = create_dataset_for(alice, name="alice-dataset", description="")

    client = admin_client_for(root)
    body = client.get(reverse("admin:datasets_dataset_change", args=[dataset.pk])).content.decode()

    assert "alice-dataset" in body
    assert 'name="_save"' not in body  # no save button: the page is read-only


def test_superuser_without_the_alias_is_scoped_like_anyone_else(
    staff: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production default: `DB_ADMIN_*` unset, so there is no cross-tenant alias and a
    superuser sees their own tenant."""
    from apps.core import admin as core_admin

    monkeypatch.setattr(core_admin, "audit_alias", lambda: None)
    root = _staff("root", superuser=True)

    assert core_admin.may_cross_tenants(root) is False


# --- Task 6: the lineage page -----------------------------------------------------------------


@pytest.mark.django_db
def test_lineage_page_lists_sources_and_consumers(
    staff: User, admin_client_for: Callable[[User], Client]
) -> None:
    with acting_as(staff):
        source = create_dataset_for(staff, name="source-ds", description="")
        derived = create_dataset_for(staff, name="derived-ds", description="")
        from apps.core.lineage import record_derivation

        record_derivation(derived, sources=[source])

    client = admin_client_for(staff)
    downstream = client.get(
        reverse("admin:datasets_dataset_lineage", args=[source.pk])
    ).content.decode()
    upstream = client.get(
        reverse("admin:datasets_dataset_lineage", args=[derived.pk])
    ).content.decode()

    assert "Used to build" in downstream
    assert "Dataset v1" in downstream  # the derived object, at the version that was written
    assert "Derived from" in upstream
    assert "Dataset v1" in upstream


@pytest.mark.django_db
def test_lineage_page_marks_a_stale_consumer(
    staff: User, admin_client_for: Callable[[User], Client]
) -> None:
    """The most valuable row in the feature: something built from a version that has moved on."""
    with acting_as(staff):
        source = create_dataset_for(staff, name="source-ds", description="")
        derived = create_dataset_for(staff, name="derived-ds", description="")
        from apps.core.lineage import record_derivation

        record_derivation(derived, sources=[source])
        source.name = "source-renamed"
        source.save()  # bumps the source to v2, leaving the edge on v1

    body = (
        admin_client_for(staff)
        .get(reverse("admin:datasets_dataset_lineage", args=[source.pk]))
        .content.decode()
    )

    assert "stale" in body
    assert "now at v2" in body


@pytest.mark.django_db
def test_lineage_page_404s_for_another_tenants_object(
    staff: User, other_user: User, admin_client_for: Callable[[User], Client]
) -> None:
    with acting_as(other_user):
        theirs = create_dataset_for(other_user, name="not-yours", description="")

    response = admin_client_for(staff).get(
        reverse("admin:datasets_dataset_lineage", args=[theirs.pk])
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_change_page_links_to_the_lineage_page(
    staff: User, admin_client_for: Callable[[User], Client]
) -> None:
    with acting_as(staff):
        dataset = create_dataset_for(staff, name="ds", description="")

    body = (
        admin_client_for(staff)
        .get(reverse("admin:datasets_dataset_change", args=[dataset.pk]))
        .content.decode()
    )

    assert reverse("admin:datasets_dataset_lineage", args=[dataset.pk]) in body
    assert "Events" in body  # pghistory's own button still renders


# --- Wiring -----------------------------------------------------------------------------------


def test_pghistory_admin_precedes_django_admin() -> None:
    """Its template overrides add the Events buttons; the first app defining a path wins."""
    apps_list = list(settings.INSTALLED_APPS)

    assert apps_list.index("pghistory.admin") < apps_list.index("django.contrib.admin")


def test_aggregate_events_page_shows_nothing_unfiltered(
    staff: User, admin_client_for: Callable[[User], Client]
) -> None:
    """It unions every event table, so it is not a page to load by accident."""
    assert settings.PGHISTORY_ADMIN_ALL_EVENTS is False
    with acting_as(staff):
        create_dataset_for(staff, name="alice-dataset", description="")

    body = (
        admin_client_for(staff).get(reverse("admin:pghistory_events_changelist")).content.decode()
    )

    assert "alice-dataset" not in body


def test_every_event_table_is_generated_from_one_base() -> None:
    """`PGHISTORY_BASE_MODEL`, not a per-model argument: the aggregate page unions these tables
    and needs the column set to match."""
    assert settings.PGHISTORY_BASE_MODEL == "apps.core.history.Event"
    for _, event_model in history.tracked_models():
        assert issubclass(event_model, history.Event)


def test_lineage_admin_is_read_only_and_not_a_base_model_admin(staff: User) -> None:
    model_admin = admin.site._registry[Lineage]
    request = _request(staff)

    assert isinstance(model_admin, LineageAdmin)
    assert not isinstance(model_admin, BaseModelAdmin)
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)


@pytest.mark.django_db
def test_lineage_admin_links_resolve_to_event_pages(staff: User) -> None:
    with acting_as(staff):
        edge = _record_edge(staff, "src")
        model_admin = admin.site._registry[Lineage]
        source = model_admin.source_link(edge)  # type: ignore[attr-defined]
        target = model_admin.target_link(edge)  # type: ignore[attr-defined]

    assert "datasetevent" in str(source)
    assert "datasetevent" in str(target)


@pytest.mark.django_db
def test_lineage_admin_renders_a_placeholder_for_an_unresolvable_version(staff: User) -> None:
    """It cannot happen — event rows are append-only and nothing hard-deletes them — but that is
    exactly the moment you want the page to render rather than raise."""
    import uuid as uuid_module

    with acting_as(staff):
        edge = _record_edge(staff, "src")
        edge.source_pgh_id = uuid_module.uuid4()
        model_admin = admin.site._registry[Lineage]
        rendered = str(model_admin.source_link(edge))  # type: ignore[attr-defined]

    assert "missing" in rendered


def test_admin_urls_follow_the_admin_enabled_switch() -> None:
    """`ADMIN_ENABLED=false` (the production default) removes `/admin/` entirely — with it the
    interactive API docs, which have no login page left to send anyone to."""
    import importlib

    from django.test import override_settings

    import config.urls

    def admin_is_mounted() -> bool:
        return any(str(pattern.pattern) == "admin/" for pattern in config.urls.urlpatterns)

    try:
        with override_settings(ADMIN_ENABLED=False):
            importlib.reload(config.urls)
            assert not admin_is_mounted()
        with override_settings(ADMIN_ENABLED=True):
            importlib.reload(config.urls)
            assert admin_is_mounted()
    finally:
        importlib.reload(config.urls)


def test_spa_catch_all_does_not_swallow_a_bare_admin_path(client: Client) -> None:
    """`/admin` without the trailing slash used to resolve to the SPA shell, so Django's
    APPEND_SLASH never got the chance to redirect it — a resolved URL is not a 404."""
    response = client.get("/admin")

    assert response.status_code in (301, 302)
    assert response["Location"].endswith("/admin/")


def test_admin_changelist_cannot_see_another_tenants_rows(staff: User, other_user: User) -> None:
    """`BaseModelAdmin.get_queryset` reads `all_objects`, whose manager applies the same ORM
    scope the API uses, on a connection the policies bind — not a third, divergent filter."""
    with acting_as(other_user):
        create_dataset_for(other_user, name="theirs", description="")

    model_admin = admin.site._registry[Dataset]
    with acting_as(staff):
        assert model_admin.get_queryset(_request(staff)).count() == 0
