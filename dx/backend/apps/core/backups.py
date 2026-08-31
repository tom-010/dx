"""Database backups: `dumpdata` → gzipped JSON → the `backups` storage.

The storage is `settings.STORAGES["backups"]`: the `S3_BACKUP_BUCKET` bucket of the object store
(versioned like the media bucket), or `backend/backups/` on disk with `MEDIA_STORAGE=local`.
Dumps are Django fixtures (natural keys, no content types/permissions/sessions), so a restore is
`migrate` + `loaddata` and works across schema-compatible versions of the app. Uploaded files are
not part of a dump — they live in the (versioned) media bucket.

CLI: `manage.py backup [--list|--prune]`, `manage.py restore <name>|--latest`.
Scheduled: `apps.core.tasks.backup_database` (CELERY_BEAT_SCHEDULE, nightly), keeps
`BACKUP_KEEP` dumps. For a production Postgres, a provider-level `pg_dump`/snapshot remains the
primary backup; this is the application-level, restore-anywhere copy.

A dump contains every user's rows, so it needs a connection that row-level security does not
apply to (DB_ROLE=migrator or admin — `./scripts/backup.sh` sets it, the nightly task runs on
the maintenance worker). As the runtime role the dump would silently be empty; `create_backup`
and `restore_backup` refuse to run instead (`CrossTenantAccessRequired`).
"""

import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog
from django.core.files import File
from django.core.files.storage import Storage, storages
from django.core.management import call_command
from django_scopes import scopes_disabled

from apps.core import rls
from apps.core.history import unversioned

log = structlog.get_logger(__name__)

BACKUP_PREFIX = "dx-"
BACKUP_SUFFIX = ".json.gz"
# Rebuilt by `migrate` / derived from the schema; dumping them causes conflicts on restore.
EXCLUDED = [
    "contenttypes",
    "auth.permission",
    "sessions",
    "admin.logentry",
    # Unmanaged views over the real event tables (which *are* dumped): they have no table
    # of their own, so serializing them fails outright.
    "pghistory.Events",
    "pghistory.MiddlewareEvents",
]


class BackupNotFound(Exception):
    pass


@dataclass(frozen=True)
class Backup:
    name: str
    size: int
    modified: datetime


def backup_storage() -> Storage:
    return storages["backups"]


def backup_name(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"


def _info(storage: Storage, name: str) -> Backup:
    return Backup(name=name, size=storage.size(name), modified=storage.get_modified_time(name))


def create_backup(*, storage: Storage | None = None) -> Backup:
    """Dump the database into a new `dx-<timestamp>.json.gz` in the backup storage."""
    rls.require_cross_tenant_access()
    storage = storage or backup_storage()
    name = backup_name()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name  # dumpdata gzips by file extension
        with scopes_disabled():  # dumpdata reads every owned model unscoped
            call_command(
                "dumpdata",
                use_natural_foreign_keys=True,
                use_natural_primary_keys=True,
                # `objects` hides soft-deleted rows; a backup that quietly drops them would
                # restore a database whose version chains end in rows that never existed.
                use_base_manager=True,
                exclude=EXCLUDED,
                output=str(path),
                verbosity=0,
            )
        with path.open("rb") as stream:
            saved = storage.save(name, File(stream))
    backup = _info(storage, saved)
    log.info("backup_created", name=backup.name, size=backup.size)
    return backup


def list_backups(*, storage: Storage | None = None) -> list[Backup]:
    """All dumps in the backup storage, newest first (names sort chronologically)."""
    storage = storage or backup_storage()
    _, files = storage.listdir("")
    names = [f for f in files if f.startswith(BACKUP_PREFIX) and f.endswith(BACKUP_SUFFIX)]
    return [_info(storage, name) for name in sorted(names, reverse=True)]


def latest_backup(*, storage: Storage | None = None) -> Backup | None:
    backups = list_backups(storage=storage)
    return backups[0] if backups else None


def restore_backup(name: str, *, storage: Storage | None = None) -> None:
    """Migrate (+ RLS policies), then load the dump. Rows with the same primary key are
    overwritten; rows that only exist in the current database are kept (loaddata never deletes).

    The load runs with the version triggers off (`unversioned()`): a dump carries each row's
    `version` and its event rows, and replaying it must reproduce them rather than bump every
    version by one and write a second event row for each.
    """
    storage = storage or backup_storage()
    if not storage.exists(name):
        raise BackupNotFound(name)
    rls.require_cross_tenant_access()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        with storage.open(name, "rb") as source, path.open("wb") as target:
            shutil.copyfileobj(source, target)
        call_command("migrate", interactive=False, verbosity=0)
        rls.sync()
        with scopes_disabled(), unversioned():
            call_command("loaddata", str(path), verbosity=0)
    log.info("backup_restored", name=name)


def prune_backups(keep: int, *, storage: Storage | None = None) -> list[str]:
    """Delete everything but the newest `keep` dumps; returns the deleted names."""
    storage = storage or backup_storage()
    stale = [b.name for b in list_backups(storage=storage)[max(keep, 0) :]]
    for name in stale:
        storage.delete(name)
    if stale:
        log.info("backups_pruned", deleted=len(stale), kept=keep)
    return stale
