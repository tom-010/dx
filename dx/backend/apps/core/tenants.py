"""Operations on a whole tenant: what one user owns, and erasing them completely.

Both need a connection that row-level security does not apply to (`DB_ROLE=migrator`/`admin`)
— the cascade has to *see* the rows. As the runtime role the rows are invisible, so a deletion
would clear nothing, and Postgres would only notice at commit, when the deferred foreign key
fires. That is exactly what happens in the Django admin, which is why `UserAdmin` refuses to
delete (`apps/accounts/admin.py`) and points here instead.

CLI: `manage.py delete_tenant <username>`. Export instead of erase: `manage.py pull_tenant`.
"""

from dataclasses import dataclass

import structlog
from django.core.files.storage import Storage
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
    # Keyed by storage key: the event tables mirror the FileField, so every version of a row
    # names a file — usually the same one, but a replaced upload leaves an older key that only
    # the history still points at, and that has to go too.
    stored: dict[str, Storage] = {}

    with scopes_disabled():
        for model in rls.isolated_models():
            owned = owned_rows(model, user)
            rows[model._meta.label] = owned.count()
            names = file_fields(model)
            for instance in owned.iterator() if names else ():
                for name in names:
                    file = getattr(instance, name)
                    if file and file.name:
                        stored.setdefault(str(file.name), file.storage)
        with transaction.atomic(), hard_delete():
            for model in rls.isolated_models():
                owned_rows(model, user).delete()
            user.delete()

    for name, storage in stored.items():
        storage.delete(name)

    erasure = Erasure(username=username, rows=rows, files=len(stored))
    log.info("tenant_erased", username=username, rows=erasure.total_rows, files=erasure.files)
    return erasure
