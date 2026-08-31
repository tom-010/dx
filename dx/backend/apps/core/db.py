"""Tenant context for the database: which user the current transaction acts for.

Two carriers, always set together:

- the Postgres session variable `app.user_id` (`settings.TENANT_GUC`) that the row-level
  security policies compare `owner_id` against (`apps/core/rls.py`), and
- `current_user_id`, a contextvar mirroring it in Python, so `OwnedModel.save()` can fill in
  `owner` and `BearerAuth` can tell that the middleware already verified the request's token.

`tenant_context()` (or the narrower `tenant_db_context()`) sets both for one transaction with
`SET LOCAL` — safe under connection reuse and PgBouncer transaction pooling, gone at commit.
Requests (`apps/core/middleware.py`) use it, and so do eagerly executed tasks.

`pin_session_tenant()` / `unpin_session_tenant()` set the same context at *session* level, for
a process that owns its connection and must not hold one transaction open for the whole job:
management commands, shells, and a Celery worker running one `tenant_task`
(`apps/core/tasks.py`). The pin is re-applied automatically when the connection is
re-established, because a reconnect would otherwise silently drop the tenant — reads would
return nothing and the job would report success over an empty database. It is process-wide
state, so it assumes one job at a time per process (Celery's prefork pool, a shell); never use
it in a request.

Clearing the variable sets it to `''`; the policies turn that into NULL, so without a context
every owned table is empty: the database fails closed.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.signals import connection_created
from django.dispatch import receiver
from django_scopes import scope

current_user_id: ContextVar[uuid.UUID | None] = ContextVar("current_user_id", default=None)


class NoTenantContext(RuntimeError):
    """An owned row is being written without an active tenant context."""


def guc_name() -> str:
    name = str(settings.TENANT_GUC)
    # Interpolated into DDL by rls.py; keep it an identifier, never something user-controlled.
    if not all(part.isidentifier() for part in name.split(".")) or "." not in name:
        raise ValueError(f"TENANT_GUC must look like 'app.user_id', got {name!r}")
    return name


def _set_config(user_id: uuid.UUID | None, *, is_local: bool) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config(%s, %s, %s)",
            [guc_name(), "" if user_id is None else str(user_id), is_local],
        )


@contextmanager
def tenant_db_context(user_id: uuid.UUID) -> Iterator[None]:
    """Transaction-scoped tenant context: `SET LOCAL app.user_id` + `current_user_id`.

    Opens a transaction (`atomic`) because `SET LOCAL` only lives inside one. On exit the
    previous value is restored explicitly: a released savepoint would otherwise carry the value
    to the end of the surrounding transaction (nested contexts, the test suite). If the block
    fails, the rollback undoes the `SET LOCAL` by itself.
    """
    previous = current_user_id.get()
    with transaction.atomic():
        _set_config(user_id, is_local=True)
        token = current_user_id.set(user_id)
        try:
            yield
        finally:
            current_user_id.reset(token)
        # Only reached without an exception (an exception rolls the block back, and with it the
        # SET LOCAL; running SQL on an aborted transaction would mask the original error).
        if not connection.needs_rollback:
            _set_config(previous, is_local=True)


@contextmanager
def tenant_context(user_id: uuid.UUID) -> Iterator[None]:
    """Both enforcement layers for one transaction: the database variable (RLS) and the ORM
    scope (`OwnedManager`). The one thing requests, tasks and tests wrap their work in."""
    with tenant_db_context(user_id), scope(user=user_id):
        yield


# --- Session-level context: commands, shells and the worker side of a tenant task -------------

_session_user_id: uuid.UUID | None = None


def set_session_tenant(user_id: uuid.UUID | None) -> None:
    """Set the variable for the whole connection (plain `SET`, not `SET LOCAL`).

    Only for a process that owns its connection until the job ends. Never call this from a
    request: the connection goes back to the pool with the tenant still attached. Prefer
    `pin_session_tenant()`, which also survives a reconnect.
    """
    _set_config(user_id, is_local=False)


def pin_session_tenant(user_id: uuid.UUID) -> None:
    """Session context that is re-applied whenever the connection is (re-)established."""
    global _session_user_id
    _session_user_id = user_id
    current_user_id.set(user_id)
    set_session_tenant(user_id)


def unpin_session_tenant() -> None:
    """Undo `pin_session_tenant`: no tenant, no re-apply.

    A broken connection is not an error here — the session variable dies with it, so the
    connection is simply dropped and the next one starts clean.
    """
    global _session_user_id
    _session_user_id = None
    current_user_id.set(None)
    try:
        set_session_tenant(None)
    except DatabaseError:
        connection.close()


@receiver(connection_created)
def _reapply_session_tenant(
    sender: object, connection: BaseDatabaseWrapper, **kwargs: object
) -> None:
    if _session_user_id is not None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config(%s, %s, false)", [guc_name(), str(_session_user_id)])
