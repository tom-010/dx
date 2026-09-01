"""Operations on a whole tenant: what one user owns, and erasing them completely.

Both need a connection that row-level security does not apply to (`DB_ROLE=migrator`/`admin`)
— the cascade has to *see* the rows. As the runtime role the rows are invisible, so a deletion
would clear nothing, and Postgres would only notice at commit, when the deferred foreign key
fires. That is exactly what happens in the Django admin, which is why `UserAdmin` refuses to
delete (`apps/accounts/admin.py`) and points here instead.

CLI: `manage.py delete_tenant <username>`. Export instead of erase: `manage.py pull_tenant`.
"""

import zipfile
from dataclasses import dataclass
from pathlib import Path

import structlog
from django.core.files.base import ContentFile
from django.core.files.storage import Storage, default_storage
from django.db import models, transaction
from django_scopes import scopes_disabled

from apps.accounts.models import User
from apps.core import rls
from apps.core.history import hard_delete

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Erasure:
    """What `delete_tenant` removed: rows per table, plus distinct stored files."""

    username: str
    rows: dict[str, int]
    files: int

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())


def file_fields(model: type[models.Model]) -> list[str]:
    return [field.name for field in model._meta.fields if isinstance(field, models.FileField)]


def owned_rows(model: type[models.Model], user: User) -> models.QuerySet[models.Model]:
    """Every row of `model` belonging to `user` — soft-deleted ones included.

    `_base_manager`, not `objects`: the default manager hides soft-deleted rows and (for owned
    models) applies the ORM scope. Neither is wanted here. Erasure has to reach rows the
    application has stopped showing, and an export that quietly left them out would be a false
    answer to "what do you hold about me".
    """
    return model._base_manager.filter(owner=user)


def tenant_summary(user: User) -> dict[str, int]:
    """Row count per table for `user` — the preview before an irreversible delete.

    Counts everything the policy protects, not just the live rows: the version history and the
    lineage edges go too (`rls.isolated_models`).
    """
    rls.require_cross_tenant_access()
    with scopes_disabled():
        return {
            model._meta.label: owned_rows(model, user).count() for model in rls.isolated_models()
        }


def delete_tenant(user: User) -> Erasure:
    """Erase a user and everything they own: rows first, then the files those rows referenced.

    Order matters. The database transaction is what must be atomic; files are deleted only
    after it commits, because a rollback that had already deleted them would leave rows
    pointing at missing objects — while the reverse leaves a harmless orphan in the bucket.

    This is the one place that really deletes (`hard_delete()`): the version history is the
    erased user's data too, so leaving it behind would defeat the point. It is also why files
    survive an ordinary delete — this is where they are finally reclaimed.
    """
    rls.require_cross_tenant_access()
    username = user.get_username()
    rows: dict[str, int] = {}
    stored = tenant_files(user)

    with scopes_disabled():
        for model in rls.isolated_models():
            rows[model._meta.label] = owned_rows(model, user).count()
        with transaction.atomic(), hard_delete():
            for model in rls.isolated_models():
                owned_rows(model, user).delete()
            user.delete()

    for name, storage in stored.items():
        storage.delete(name)

    erasure = Erasure(username=username, rows=rows, files=len(stored))
    log.info("tenant_erased", username=username, rows=erasure.total_rows, files=erasure.files)
    return erasure


# --- The whole tenant as one file: rows plus the objects they point at ---------------------------
#
# `pull_tenant --with-files` / `load_tenant`. The fixture alone restores the rows; without the
# files those rows name keys that are not in the bucket, which is a restore in name only.

#: What a tenant archive holds.
ARCHIVE_FIXTURE = "tenant.json"
ARCHIVE_FILES = "files/"


class TenantArchiveError(Exception):
    """The archive cannot be read, or restoring it would not reproduce the tenant."""


def tenant_files(user: User) -> dict[str, Storage]:
    """Every stored file `user`'s rows point at, keyed by storage key.

    The event tables mirror the `FileField`, so every *version* of a row names a file — usually
    the same one, but a replaced upload leaves an older key that only the history still points
    at. Erasure has to reclaim those, and an archive has to carry them, or restoring an old
    version would resolve to nothing.
    """
    stored: dict[str, Storage] = {}
    with scopes_disabled():
        for model in rls.isolated_models():
            names = file_fields(model)
            for instance in owned_rows(model, user).iterator() if names else ():
                for name in names:
                    file = getattr(instance, name)
                    if file and file.name:
                        stored.setdefault(str(file.name), file.storage)
    return stored


def write_archive(path: Path, fixture: str, files: dict[str, Storage]) -> int:
    """Write the fixture and every file it references into one zip; returns the file count.

    A key the bucket no longer has is logged and skipped rather than fatal: object storage is
    not part of the database transaction, so a gap is possible, and an archive of everything
    else is still worth having.
    """
    written = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(ARCHIVE_FIXTURE, fixture)
        for key, storage in files.items():
            try:
                with storage.open(key) as stored:
                    archive.writestr(ARCHIVE_FILES + key, stored.read())
            except OSError, ValueError:
                log.warning("tenant_archive_file_missing", key=key)
                continue
            written += 1
    return written


def unpack_archive(path: Path, into: Path) -> tuple[Path, int]:
    """Restore an archive: files back to the keys they came from, fixture into `into`.

    Keys are restored *exactly*, because the rows in the fixture name them: an existing object
    is replaced rather than saved beside it as `…_a1b2c3.pdf`, which is what `Storage.save`
    does with a name it already has. Everything goes back through the default storage, the one
    every `FileField` in this project uses (`owned_upload_path`).
    """
    fixture = into / ARCHIVE_FIXTURE
    restored = 0
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if ARCHIVE_FIXTURE not in names:
            raise TenantArchiveError(f"{path} is not a tenant archive: no {ARCHIVE_FIXTURE}")
        fixture.write_bytes(archive.read(ARCHIVE_FIXTURE))
        for name in names:
            if not name.startswith(ARCHIVE_FILES) or name.endswith("/"):
                continue
            key = name[len(ARCHIVE_FILES) :]
            # The archive is data, not instructions: a key from it must stay inside the store.
            if not key or key.startswith("/") or ".." in Path(key).parts:
                raise TenantArchiveError(f"refusing a key that leaves the store: {key!r}")
            if default_storage.exists(key):
                default_storage.delete(key)
            saved = default_storage.save(key, ContentFile(archive.read(name)))
            if saved != key:  # pragma: no cover - only if the key was taken again mid-restore
                raise TenantArchiveError(
                    f"storage renamed {key!r} to {saved!r}; the restored rows would point at "
                    "a key that does not exist"
                )
            restored += 1
    log.info("tenant_archive_unpacked", files=restored)
    return fixture, restored
