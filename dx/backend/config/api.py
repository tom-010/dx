"""Root django-ninja API. Feature routers are registered here.

The OpenAPI spec of this API is the contract with the frontend: export it with
`./scripts/sync_schema.sh` (→ `openschema.json` → orval → `frontend/src/api/`).

Every operation requires `Authorization: Bearer <token>` (`apps.accounts.auth.BearerAuth`)
unless it opts out with `auth=None` — `apps/core/tests/test_security.py` keeps the list of
public operations. The interactive docs need a staff session (admin login).
"""

from django.contrib.admin.views.decorators import staff_member_required
from ninja import NinjaAPI
from ninja.operation import Operation

from apps.accounts.api import router as accounts_router
from apps.accounts.auth import BearerAuth
from apps.core.api import router as core_router
from apps.core.api import tasks_router
from apps.datasets.api import router as datasets_router
from apps.documents.api import router as documents_router
from apps.gallery.api import router as gallery_router
from config.errors import install_exception_handlers


class Api(NinjaAPI):
    def get_openapi_operation_id(self, operation: Operation) -> str:
        # Plain view names (`list_datasets`) instead of ninja's module-qualified default, so the
        # generated client gets readable names (`useListDatasets`). Names must be globally unique;
        # `apps/core/tests/test_openapi.py` enforces that.
        return operation.view_func.__name__


api = Api(title="dx API", version="0.1.0", auth=BearerAuth(), docs_decorator=staff_member_required)
install_exception_handlers(api)

api.add_router("/", core_router)
api.add_router("/", tasks_router)
api.add_router("/", accounts_router)
api.add_router("/", datasets_router)
api.add_router("/", documents_router)
api.add_router("/", gallery_router)
# needle: routers (manage.py startmodule inserts new routers above this line)
