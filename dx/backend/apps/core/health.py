"""Health checks behind `GET /api/health` (liveness) and `GET /api/ready` (readiness).

Liveness answers as long as the process serves requests; readiness runs `readiness()` and is
what orchestrators/compose should gate on: the database is reachable *and* migrated, row-level
security is in place and applies to this connection, the Celery broker answers (unless tasks
run eagerly), and the object store buckets exist.
"""

from dataclasses import dataclass

from django.conf import settings
from django.core.files.storage import storages
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.core import rls
from apps.core.storage import bucket_exists, s3_storage
from config.celery import app as celery_app
from config.env import env


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


def check_database() -> Check:
    """Reachable, and through what: the web process should say "pooled" (PgBouncer), a
    worker or command "direct" (config/env.py `DB_POOLED`)."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        return Check("database", False, f"{type(exc).__name__}: {exc}")
    db = settings.DATABASES["default"]
    via = "pooled" if env.pooled() else "direct"
    return Check("database", True, f"{db['HOST']}:{db['PORT']} ({via})")


def check_migrations() -> Check:
    try:
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception as exc:  # noqa: BLE001
        return Check("migrations", False, f"{type(exc).__name__}: {exc}")
    if pending:
        return Check("migrations", False, f"{len(pending)} unapplied migration(s)")
    return Check("migrations", True)


def check_rls() -> Check:
    """Every table holding tenant data — owned models, their history, the lineage graph —
    carries its policy, and the process runs as a role the policies apply to: a web process
    connected as the table owner or a superuser would see every tenant."""
    try:
        problems = rls.verify()
        bypass = rls.connection_bypasses_rls()
        role = rls.current_role()
    except Exception as exc:  # noqa: BLE001
        return Check("rls", False, f"{type(exc).__name__}: {exc}")
    if problems:
        return Check("rls", False, "; ".join(problems))
    if bypass is not None:
        return Check("rls", False, f"{bypass} — row-level security does not apply (DB_ROLE)")
    return Check("rls", True, f"{len(rls.isolated_tables())} tables, role {role}")


def check_broker() -> Check:
    if settings.CELERY_TASK_ALWAYS_EAGER:
        return Check("celery", True, "eager mode, no broker")
    try:
        with celery_app.connection_for_write() as conn:
            conn.ensure_connection(max_retries=1, interval_start=0, interval_step=1)
    except Exception as exc:  # noqa: BLE001
        return Check("celery", False, f"{type(exc).__name__}: {exc}")
    return Check("celery", True)


def check_storage(alias: str) -> Check:
    name = f"storage:{alias}"
    storage = s3_storage(alias)
    if storage is None:
        return Check(name, True, f"local disk ({storages[alias].__class__.__name__})")
    try:
        if not bucket_exists(storage.connection.meta.client, storage.bucket_name):
            return Check(name, False, f"bucket {storage.bucket_name!r} missing (ensure_bucket)")
    except Exception as exc:  # noqa: BLE001
        return Check(name, False, f"{type(exc).__name__}: {exc}")
    return Check(name, True, f"bucket {storage.bucket_name!r}")


def readiness() -> list[Check]:
    return [
        check_database(),
        check_migrations(),
        check_rls(),
        check_broker(),
        check_storage("default"),
        check_storage("backups"),
    ]
