"""Helpers for the test suite (conventions: backend/conftest.py, CLAUDE.md "Testing").

    from apps.core.testing import acting_as

    with acting_as(user):
        dataset = create_dataset(user, name="mine")

Service functions and the ORM only work inside a tenant context — a request gets it from
`TenantMiddleware`, a task from `tenant_task`, a test from `acting_as(user)`.
"""

from contextlib import AbstractContextManager

from apps.accounts.models import User
from apps.core.db import tenant_context


def acting_as(user: User) -> AbstractContextManager[None]:
    """Tenant context (ORM scope + database variable) of `user` for the `with` block."""
    return tenant_context(user.pk)
