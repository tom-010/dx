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

log = structlog.get_logger(__name__)

BACKUP_PREFIX = "dx-"
BACKUP_SUFFIX = ".json.gz"
# Rebuilt by `migrate` / derived from the schema; dumping them causes conflicts on restore.
EXCLUDED = ["contenttypes", "auth.permission", "sessions", "admin.logentry"]


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
    storage = storage or backup_storage()
    name = backup_name()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name  # dumpdata gzips by file extension
        call_command(
            "dumpdata",
            use_natural_foreign_keys=True,
            use_natural_primary_keys=True,
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
    """Migrate, then load the dump. Rows with the same primary key are overwritten; rows that
    only exist in the current database are kept (loaddata never deletes)."""
    storage = storage or backup_storage()
    if not storage.exists(name):
        raise BackupNotFound(name)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        with storage.open(name, "rb") as source, path.open("wb") as target:
            shutil.copyfileobj(source, target)
        call_command("migrate", interactive=False, verbosity=0)
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
