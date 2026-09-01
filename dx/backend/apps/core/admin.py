"""Auto-registration for the Django admin.

The admin is a **dev-only** staff UI for looking at rows: `ADMIN_ENABLED` defaults to `DEBUG`,
`/admin/` does not resolve in production, and `register_all` registers nothing there either — an
admin page that cannot be reached should not be built. There are no per-model admin classes:
every app's `admin.py` is three lines, `register_all(models)`, so a new model shows up without
anyone maintaining a page for it.

What that deliberately costs, since the pages are Django's defaults:

- the delete button issues a real `DELETE`, which the `no_hard_delete` trigger rejects — deleting
  is `obj.soft_delete()` (see CLAUDE.md "Versioning, history and lineage");
- the event tables are append-only, so saving one of their change forms fails the same way;
- `User` gets a plain form, i.e. the password field is a raw hash box, and deleting a user from
  here would report success and then fail at the foreign key on commit (`manage.py delete_tenant`
  does it properly).

Administer real data with `manage.py shell_as` / `shell_admin`, not through this.

`AdminTenantMiddleware` (apps/core/middleware.py) is what makes the pages resolve at all: without
a tenant context `OwnedManager` raises and row-level security hides every row.
"""

from types import ModuleType

from django.apps import apps
from django.conf import settings
from django.contrib import admin
from django.db.models import Model

from apps.core import models as core_models


def register_all(models_module: ModuleType) -> None:
    """Register every model *this app* defines in `models_module` with the default admin.

    A no-op when the admin is disabled (production). Only the app's own models: `dir()` also
    sees whatever the module imported, and registering another app's model from here would
    either duplicate its page or raise `AlreadyRegistered` at boot. The event models pghistory
    generates carry the app's label, so they are registered too.
    """
    if not settings.ADMIN_ENABLED:
        return
    app_config = apps.get_containing_app_config(models_module.__name__)
    label = app_config.label if app_config is not None else None
    for name in dir(models_module):
        if name.startswith("_"):
            continue
        candidate = getattr(models_module, name)
        if not isinstance(candidate, type) or not issubclass(candidate, Model):
            continue
        # `django.db.models.Model` itself has no `_meta`; abstract bases have nothing to show.
        meta = getattr(candidate, "_meta", None)
        if meta is None or meta.abstract or meta.app_label != label:
            continue
        admin.site.register(candidate)


register_all(core_models)
