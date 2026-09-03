"""`manage.py rebuild_timeline` — the migration path for data that predates an event type."""

from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.core.testing import acting_as
from apps.documents.api import store_documents
from apps.documents.timeline_events import DOCUMENT_UPLOADED
from apps.timeline.models import TimelineEvent

pytestmark = [pytest.mark.django_db, pytest.mark.cross_tenant]


@pytest.fixture(autouse=True)
def media_root(settings: Settings, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = tmp_path


def upload(owner: User, name: str) -> None:
    with acting_as(owner):
        # Distinct bytes per name — the library files one document per distinct content.
        content = f"%PDF {name}".encode()
        store_documents(owner, [SimpleUploadedFile(name, content, content_type="application/pdf")])


def test_it_rebuilds_every_tenants_feed(user: User, other_user: User) -> None:
    upload(user, "mine.pdf")
    upload(other_user, "theirs.pdf")
    with acting_as(user):
        TimelineEvent.all_objects.all().hard_delete()  # a database from before the app existed

    call_command("rebuild_timeline")

    for owner, title in ((user, "mine.pdf"), (other_user, "theirs.pdf")):
        with acting_as(owner):
            assert [e.title for e in TimelineEvent.objects.all()] == [title]


def test_a_dry_run_writes_nothing(user: User) -> None:
    upload(user, "mine.pdf")
    with acting_as(user):
        TimelineEvent.all_objects.all().hard_delete()

    call_command(
        "rebuild_timeline", "--type", DOCUMENT_UPLOADED, "--user", user.username, "--dry-run"
    )

    with acting_as(user):
        assert TimelineEvent.all_objects.count() == 0
