"""The admin is Django's defaults over auto-registered models (apps/core/admin.py).

There are no admin classes to test any more. What is still worth a test is that the pages
*open*: every one of them reads owned data, which raises without the tenant context
`AdminTenantMiddleware` opens, and returns nothing if row-level security is not satisfied.
"""

from types import ModuleType

import pytest
from django.contrib import admin
from django.db.models import Model
from django.test import Client, override_settings

from apps.accounts.models import User
from apps.core.admin import register_all
from apps.core.testing import acting_as
from apps.datasets.api import create_dataset_for


def test_every_concrete_model_is_registered() -> None:
    """`register_all` runs on import of each app's `admin.py`, and the event models pghistory
    generates are attributes of the same `models.py` — so they are registered too."""
    registered = {model._meta.label for model in admin.site._registry}

    assert {"datasets.Dataset", "datasets.DatasetEvent", "accounts.User", "core.Lineage"} <= (
        registered
    )


@pytest.fixture
def admin_client_for(client: Client, staff_user: User) -> Client:
    staff_user.is_superuser = True
    staff_user.save()
    client.force_login(staff_user)
    return client


@pytest.mark.django_db
def test_every_registered_changelist_opens(admin_client_for: Client) -> None:
    """Without a tenant context `OwnedManager` raises and the page is a 500."""
    for model in admin.site._registry:
        meta = model._meta
        url = f"/admin/{meta.app_label}/{meta.model_name}/"

        assert admin_client_for.get(url).status_code == 200, url


@pytest.mark.django_db
def test_a_changelist_shows_only_the_staff_users_own_rows(
    admin_client_for: Client, user: User
) -> None:
    """Tenant == user: the admin session is the tenant, so another user's rows are not there."""
    with acting_as(user):
        create_dataset_for(user, name="alice-only")

    response = admin_client_for.get("/admin/datasets/dataset/")

    assert response.status_code == 200
    assert b"alice-only" not in response.content


def test_register_all_skips_what_the_module_only_imported() -> None:
    """`dir()` sees imported names too. Registering another app's model from here would raise
    `AlreadyRegistered` at boot — it already has a page from its own `admin.py`. `Model` itself
    has no `_meta`, which would be an AttributeError."""
    foreign = ModuleType("apps.datasets.models")
    foreign.User = User  # type: ignore[attr-defined]  # belongs to accounts
    foreign.Model = Model  # type: ignore[attr-defined]  # the base class, not a model

    register_all(foreign)  # must not raise, and must register nothing

    assert admin.site._registry[User].__class__ is admin.ModelAdmin


@override_settings(ADMIN_ENABLED=False)
def test_register_all_does_nothing_when_the_admin_is_disabled() -> None:
    """Production: `/admin/` does not resolve, so nothing should be registered for it either."""
    before = dict(admin.site._registry)

    register_all(ModuleType("apps.datasets.models"))

    assert admin.site._registry == before
