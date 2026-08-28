"""Health checks behind `GET /api/health` (liveness) and `GET /api/ready` (readiness).

Liveness answers as long as the process serves requests; readiness runs `readiness()` and is
what orchestrators/compose should gate on: the database is reachable *and* migrated, the Celery
broker answers (unless tasks run eagerly), and the object store buckets exist.
"""

from dataclasses import dataclass

from django.conf import settings
from django.core.files.storage import storages
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.core.storage import bucket_exists, s3_storage
from config.celery import app as celery_app


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


def check_database() -> Check:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        return Check("database", False, f"{type(exc).__name__}: {exc}")
    return Check("database", True)


def check_migrations() -> Check:
    try:
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception as exc:  # noqa: BLE001
        return Check("migrations", False, f"{type(exc).__name__}: {exc}")
    if pending:
        return Check("migrations", False, f"{len(pending)} unapplied migration(s)")
    return Check("migrations", True)


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
        check_broker(),
        check_storage("default"),
        check_storage("backups"),
    ]
