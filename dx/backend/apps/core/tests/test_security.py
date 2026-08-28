"""Every route is private by default. Public exceptions are listed here, on purpose, with a
reason — adding an endpoint without auth fails these tests until it is listed."""

import json
from collections.abc import Iterator

import pytest
from django.test import Client
from ninja.operation import Operation

from apps.accounts.models import User
from config.api import api

PUBLIC_OPERATIONS = {
    "health_check": "liveness probe for the container / load balancer",
    "ready": "readiness probe (database, migrations, broker, storage) for orchestrators",
    "login": "issues the tokens everything else needs",
    "register": "self-service sign-up (disabled unless REGISTRATION_OPEN)",
    "download_document": "protected by a signed, expiring URL instead of a header",
    "stream_task_events": "SSE for EventSource (no headers): signed, expiring URL instead",
}


def _operations() -> Iterator[Operation]:
    # Bound routers carry the effective auth (API-level auth is applied when routers are mounted);
    # `api._routers` only holds the unmounted templates.
    for bound_router in api._get_bound_routers():
        for path_view in bound_router.path_operations.values():
            yield from path_view.operations


def _http_calls() -> list[tuple[str, str, str]]:
    """(operation id, method, concrete path) for every operation in the OpenAPI spec."""
    schema = json.loads(json.dumps(api.get_openapi_schema()))
    calls = []
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            concrete = path.replace("{", "").replace("}", "")
            for parameter in operation.get("parameters", []):
                if parameter["in"] == "path":
                    concrete = concrete.replace(parameter["name"], "1")
            calls.append((operation["operationId"], method.upper(), concrete))
    return sorted(calls)


def test_public_operations_are_exactly_the_allowlist() -> None:
    unprotected = sorted(op.view_func.__name__ for op in _operations() if not op.auth_callbacks)

    assert unprotected == sorted(PUBLIC_OPERATIONS), (
        "operation without auth — add auth or list it in PUBLIC_OPERATIONS with a reason"
    )


@pytest.mark.django_db
@pytest.mark.parametrize(("operation_id", "method", "path"), _http_calls(), ids=lambda v: str(v))
def test_anonymous_requests_are_rejected(
    client: Client, operation_id: str, method: str, path: str
) -> None:
    response = client.generic(method, path)

    if operation_id in PUBLIC_OPERATIONS:
        assert response.status_code != 401, f"{operation_id} is meant to be public"
    else:
        assert response.status_code == 401, f"{method} {path} answered {response.status_code}"
        assert response.json() == {"detail": "Unauthorized"}


@pytest.mark.django_db
def test_api_docs_require_a_staff_session(client: Client, staff_user: User) -> None:
    assert client.get("/api/docs").status_code == 302  # → admin login
    assert client.get("/api/openapi.json").status_code == 302

    client.force_login(staff_user)
    assert client.get("/api/docs").status_code == 200
    assert client.get("/api/openapi.json").status_code == 200


def test_spa_shell_and_static_assets_stay_public(client: Client) -> None:
    """The login page lives in the SPA, so the HTML shell itself cannot require a token."""
    assert client.get("/login").status_code in (200, 503)  # 503 = frontend not built
    assert client.get("/admin/login/").status_code == 200
