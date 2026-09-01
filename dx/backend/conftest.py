"""Shared pytest fixtures and conventions — the testing strategy is described in CLAUDE.md.

- `client` (pytest-django) is anonymous. The API rejects it; use it for public endpoints and
  401 checks.
- `auth_client` acts as `user`; `client_for(other_user)` builds a second identity for
  ownership/isolation tests ("B must not see A's things").
- `apps.core.testing.acting_as(user)` opens the tenant context (ORM scope + database
  variable) for code called directly, outside a request: `with acting_as(user):
  create_dataset(user, ...)`. Requests get it from the middleware.
- Every database test runs as the runtime role `app_user` (`SET ROLE` on the test connection),
  so row-level security is enforced exactly as in production; the policies are created on the
  test database right after the migrations. `@pytest.mark.cross_tenant` keeps the owner's
  view for the backup/restore tooling.
- Files named `test_api.py` / `test_commands.py` get the `api` / `infra` marker automatically,
  so `pytest -m "not api"` runs only the fast service/unit tests.
"""

from collections.abc import Callable, Iterator

import pytest
from django.db import DatabaseError, connection
from django.test import Client
from pytest_django import DjangoDbBlocker

from apps.accounts.api import issue_access_token
from apps.accounts.models import User
from apps.core import rls

AUTO_MARKERS = {"test_api.py": "api", "test_commands.py": "infra"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        marker = AUTO_MARKERS.get(item.path.name)
        if marker is not None:
            item.add_marker(marker)


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup: None, django_db_blocker: DjangoDbBlocker) -> None:
    """Row-level security on the test database: policies + grants are DDL outside the
    migrations (`manage.py rls_sync` in a deployment)."""
    with django_db_blocker.unblock():
        rls.sync()


@pytest.fixture(autouse=True)
def rls_enforced(request: pytest.FixtureRequest) -> Iterator[None]:
    """Database tests act as `app_user`, the role the policies apply to. The suite connects as
    the table owner (it has to create and migrate the database), which would bypass them."""
    uses_db = request.node.get_closest_marker("django_db") is not None or any(
        name in request.fixturenames for name in ("db", "transactional_db")
    )
    if not uses_db or request.node.get_closest_marker("cross_tenant") is not None:
        yield
        return
    request.getfixturevalue("db")  # the test transaction must be open before switching roles
    with connection.cursor() as cursor:
        cursor.execute(f"SET ROLE {rls.APP_ROLE}")
    yield
    try:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
    except DatabaseError:
        pass  # aborted transaction: the rollback undoes the SET ROLE anyway


@pytest.fixture
def user(db: None) -> User:
    return User.objects.create_user("alice", "alice@example.com", "correct horse battery")


@pytest.fixture
def other_user(db: None) -> User:
    return User.objects.create_user("bob", "bob@example.com", "another password")


@pytest.fixture
def staff_user(db: None) -> User:
    return User.objects.create_user("staff", "staff@example.com", "staff password", is_staff=True)


@pytest.fixture
def client_for() -> Callable[[User], Client]:
    """`client_for(some_user)` → API client sending that user's bearer token."""

    def make(user: User) -> Client:
        return Client(headers={"Authorization": f"Bearer {issue_access_token(user)}"})

    return make


@pytest.fixture
def auth_client(user: User, client_for: Callable[[User], Client]) -> Client:
    return client_for(user)
