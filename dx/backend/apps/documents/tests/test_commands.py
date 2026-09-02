"""`manage.py prune_contents` and `manage.py gc_blobs` (logic in `apps/documents/ops.py`)."""

import pytest
from click.testing import CliRunner

from apps.accounts.models import User
from apps.core.testing import acting_as
from apps.documents import snapshot
from apps.documents.management.commands import gc_blobs, prune_contents
from apps.documents.models import Blob, DocumentContent
from apps.documents.tests.conftest import FakeStrategy, upload

pytestmark = [pytest.mark.django_db, pytest.mark.cross_tenant]


def test_prune_contents_retires_old_runs_and_keeps_the_current(
    user: User, fake: FakeStrategy
) -> None:
    document = upload(user)
    with acting_as(user):
        old = snapshot.extract_now(document, fake)
        current = snapshot.extract_now(document, fake)
    runner = CliRunner()

    dry = runner.invoke(prune_contents.command, ["--days", "0", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "report.fake" in dry.output and "retired" not in dry.output

    real = runner.invoke(prune_contents.command, ["--days", "0"])
    assert real.exit_code == 0, real.output
    assert "retired 1 contents" in real.output
    with acting_as(user):
        assert not DocumentContent.objects.filter(pk=old.pk).exists()
        assert DocumentContent.objects.get(pk=current.pk).is_current

    again = runner.invoke(prune_contents.command, ["--days", "0"])
    assert again.exit_code == 0 and "nothing to prune" in again.output


def test_gc_blobs_retires_orphans_only(user: User) -> None:
    document = upload(user)
    with acting_as(user):
        orphan = snapshot.store_bytes(user.pk, b"nobody points here", "text/plain")
    runner = CliRunner()

    dry = runner.invoke(gc_blobs.command, ["--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert orphan.sha256[:16] in dry.output
    with acting_as(user):
        assert Blob.objects.filter(pk=orphan.pk).exists()

    real = runner.invoke(gc_blobs.command, [])
    assert real.exit_code == 0, real.output
    assert "retired 1 blob(s)" in real.output
    with acting_as(user):
        assert list(Blob.objects.all()) == [document.source_blob]
        assert Blob.all_objects.get(pk=orphan.pk).deleted_at is not None
