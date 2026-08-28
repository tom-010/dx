"""Shared pytest fixtures and conventions — the testing strategy is described in CLAUDE.md.

- `client` (pytest-django) is anonymous. The API rejects it; use it for public endpoints and
  401 checks.
- `auth_client` acts as `user`; `client_for(other_user)` builds a second identity for
  ownership/isolation tests ("B must not see A's things").
- Files named `test_api.py` / `test_commands.py` get the `api` / `infra` marker automatically,
  so `pytest -m "not api"` runs only the fast service/unit tests.
"""

from collections.abc import Callable

import pytest
from django.test import Client

from apps.accounts.models import User
from apps.accounts.services import issue_access_token

AUTO_MARKERS = {"test_api.py": "api", "test_commands.py": "infra"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        marker = AUTO_MARKERS.get(item.path.name)
        if marker is not None:
            item.add_marker(marker)


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
