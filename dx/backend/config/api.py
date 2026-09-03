"""Root django-ninja API. Feature routers are registered here.

The OpenAPI spec of this API is the contract with the frontend: export it with
`./scripts/sync_schema.sh` (→ `openschema.json` → orval → `frontend/src/api/`).

Every operation requires `Authorization: Bearer <token>` (`apps.accounts.api.BearerAuth`)
unless it opts out with `auth=None` — `apps/core/tests/test_security.py` keeps the list of
public operations. The interactive docs need a staff session (admin login).
"""

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from ninja import NinjaAPI
from ninja.operation import Operation

from apps.accounts.api import BearerAuth
from apps.accounts.api import router as accounts_router
from apps.core.api import history_router, tasks_router
from apps.core.api import router as core_router
from apps.datasets.api import router as datasets_router
from apps.documents.api import router as documents_router
from apps.gallery.api import router as gallery_router
from apps.notes.api import router as notes_router
from apps.notifications.api import router as notifications_router
from apps.timeline.api import router as timeline_router
from config.errors import install_exception_handlers


class Api(NinjaAPI):
    def get_openapi_operation_id(self, operation: Operation) -> str:
        # Plain view names (`list_datasets`) instead of ninja's module-qualified default, so the
        # generated client gets readable names (`useListDatasets`). Names must be globally unique;
        # `apps/core/tests/test_openapi.py` enforces that.
        return operation.view_func.__name__


# The interactive docs and the served spec need a staff session, and the only way to get one is
# the admin login — so they exist exactly where the admin does (config/urls.py). Building the
# spec does not go through these URLs (`manage.py export_openapi_schema` reads `api` in-process),
# so `./scripts/sync_schema.sh` and the tests are unaffected.
_docs = (
    {"docs_decorator": staff_member_required}
    if settings.ADMIN_ENABLED
    else {
        "docs_url": None,
        "openapi_url": None,
    }
)

api = Api(title="dx API", version="0.1.0", auth=BearerAuth(), **_docs)
install_exception_handlers(api)

api.add_router("/", core_router)
api.add_router("/", tasks_router)
api.add_router("/", history_router)
api.add_router("/", accounts_router)
api.add_router("/", datasets_router)
api.add_router("/", documents_router)
api.add_router("/", gallery_router)
api.add_router("/", notes_router)
api.add_router("/", notifications_router)
api.add_router("/", timeline_router)
# needle: routers (manage.py newapp inserts new routers above this line)
