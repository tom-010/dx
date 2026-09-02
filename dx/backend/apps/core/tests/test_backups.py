"""Database dumps in a storage (apps/core/backups.py); the CLI is covered in test_commands.py.

A dump holds every tenant's rows, so these tests keep the table owner's database role
(`cross_tenant`): as the runtime role, row-level security would hide most of what is dumped
and `create_backup` refuses to write a partial dump at all.
"""

from pathlib import Path

import pytest
from django.core.files.storage import FileSystemStorage

from apps.accounts.models import User
from apps.core import backups
from apps.core.testing import acting_as
from apps.datasets.api import create_dataset_for
from apps.datasets.models import Dataset

pytestmark = [pytest.mark.django_db, pytest.mark.cross_tenant]


@pytest.fixture
def storage(tmp_path: Path) -> FileSystemStorage:
    return FileSystemStorage(location=str(tmp_path))


def test_backup_round_trip(user: User, other_user: User, storage: FileSystemStorage) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="keep me", row_count=3)
    with acting_as(other_user):
        theirs = create_dataset_for(other_user, name="theirs too")

    backup = backups.create_backup(storage=storage)

    assert backup.name.startswith("dx-") and backup.name.endswith(".json.gz")
    assert backup.size > 0
    assert backups.list_backups(storage=storage) == [backup]

    with acting_as(user):  # the one sanctioned real delete: test teardown
        Dataset.all_objects.all().hard_delete()
    backups.restore_backup(backup.name, storage=storage)

    with acting_as(user):
        restored = Dataset.objects.get(pk=dataset.pk)
        assert (restored.name, restored.row_count, restored.owner) == ("keep me", 3, user)
    with acting_as(other_user):
        assert Dataset.objects.get(pk=theirs.pk).owner == other_user  # every tenant is in it


def test_list_and_prune_keep_the_newest(
    storage: FileSystemStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = iter(
        [
            "dx-2026-01-01T00-00-00Z.json.gz",
            "dx-2026-01-02T00-00-00Z.json.gz",
            "dx-2026-01-03T00-00-00Z.json.gz",
        ]
    )
    monkeypatch.setattr(backups, "backup_name", lambda now=None: next(names))
    for _ in range(3):
        backups.create_backup(storage=storage)

    assert [b.name for b in backups.list_backups(storage=storage)] == [
        "dx-2026-01-03T00-00-00Z.json.gz",
        "dx-2026-01-02T00-00-00Z.json.gz",
        "dx-2026-01-01T00-00-00Z.json.gz",
    ]
    assert backups.latest_backup(storage=storage) is not None

    deleted = backups.prune_backups(1, storage=storage)

    assert deleted == ["dx-2026-01-02T00-00-00Z.json.gz", "dx-2026-01-01T00-00-00Z.json.gz"]
    assert [b.name for b in backups.list_backups(storage=storage)] == [
        "dx-2026-01-03T00-00-00Z.json.gz"
    ]


def test_restore_unknown_backup_raises(storage: FileSystemStorage) -> None:
    with pytest.raises(backups.BackupNotFound):
        backups.restore_backup("dx-nope.json.gz", storage=storage)
    assert backups.latest_backup(storage=storage) is None
