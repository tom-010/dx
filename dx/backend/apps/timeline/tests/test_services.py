"""The projection's contract: record is an upsert, deletes are soft, rebuild is idempotent."""

import datetime as dt
import uuid
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.core.testing import acting_as
from apps.documents.api import store_documents
from apps.documents.models import Document
from apps.documents.testing import uploadable
from apps.documents.timeline_events import DOCUMENT_UPLOADED
from apps.timeline import services
from apps.timeline.contracts import (
    EventData,
    EventType,
    EventTypeRegistry,
    InvalidEventType,
    UnknownEventType,
    registry,
)
from apps.timeline.models import DatePrecision, EventKind, TimelineEvent
from apps.timeline.testing import assert_event, assert_no_event

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def media_root(settings: Settings, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = tmp_path


def a_document(user: User, name: str = "scan.pdf") -> Document:
    # Distinct bytes per name: the library files one document per distinct content, so two
    # uploads of the same bytes are one row and these tests would be counting the wrong thing.
    file = SimpleUploadedFile(name, f"%PDF-1.4 {name}".encode(), content_type="application/pdf")
    return store_documents(user, [file])[0]


def test_uploading_a_document_writes_one_event(user: User) -> None:
    with acting_as(user):
        document = a_document(user)

        event = assert_event(document, DOCUMENT_UPLOADED, title="scan.pdf")
        assert event.kind == EventKind.TECHNICAL
        assert event.source_model == "documents.document"
        assert event.source_id == document.pk
        assert event.owner_id == user.pk
        assert event.payload["mime_type"] == "application/pdf"


def test_recording_again_updates_the_same_row(user: User) -> None:
    """The rename case: one event, one id, one more version — never a second card."""
    with acting_as(user):
        document = a_document(user)
        first = assert_event(document, DOCUMENT_UPLOADED)

        document.title = "Arztbrief Dr. Müller"
        document.save(operation=None, sources=[])
        again = services.record(DOCUMENT_UPLOADED, document)

        assert again.pk == first.pk
        assert again.title == "Arztbrief Dr. Müller"
        assert again.version == first.version + 1
        assert TimelineEvent.objects.count() == 1


def test_remove_is_soft_and_record_revives_the_same_event(user: User) -> None:
    with acting_as(user):
        document = a_document(user)
        event = assert_event(document, DOCUMENT_UPLOADED)

        assert services.remove(document) == 1
        assert_no_event(document)
        assert TimelineEvent.all_objects.deleted().count() == 1

        revived = services.record(DOCUMENT_UPLOADED, document)
        assert revived.pk == event.pk
        assert revived.deleted_at is None


def test_the_event_records_what_it_was_derived_from(user: User) -> None:
    """A card is computed from a row, so the lineage graph says so and `stale_derivations`
    can find the cards a change invalidated."""
    with acting_as(user):
        document = a_document(user)
        event = assert_event(document, DOCUMENT_UPLOADED)

        sources = event.sources(Document)
        assert [version.to_object().pk for version in sources] == [document.pk]


def test_deleting_a_document_retires_its_event(user: User, auth_client: Client) -> None:
    with acting_as(user):
        document = a_document(user)

    assert auth_client.delete(f"/api/documents/{document.pk}").status_code == 204

    with acting_as(user):
        assert_no_event(document)


def test_extraction_retitles_the_card(user: User) -> None:
    """`snapshot.switch_current` gives a document the title the extraction read; the card that
    already exists has to follow, not be duplicated."""
    from apps.documents import snapshot  # noqa: PLC0415 - only this test needs the pipeline

    with acting_as(user):
        file = SimpleUploadedFile("notes.txt", b"Kickoff\n\nAgreed.", content_type="text/plain")
        # Text is extractable but not uploadable (`documents.api.SUPPORTED_UPLOAD_FORMATS`);
        # this test wants the cheapest extraction there is, not the picker's rule.
        with uploadable("text/plain"):
            document = store_documents(user, [file])[0]
        content = snapshot.start_extraction(document)
        assert content is not None
        document.refresh_from_db()

        events = TimelineEvent.objects.filter(source_id=document.pk)
        assert events.count() == 1
        assert events.get().title == document.title


# --- normalization, validation, the registry -------------------------------------------------


@pytest.mark.parametrize(
    ("precision", "expected"),
    [
        (DatePrecision.DATETIME, dt.datetime(2019, 3, 12, 22, 30, tzinfo=dt.UTC)),
        (DatePrecision.DAY, dt.datetime(2019, 3, 12, 12, 0, tzinfo=dt.UTC)),
        (DatePrecision.MONTH, dt.datetime(2019, 3, 1, 12, 0, tzinfo=dt.UTC)),
        (DatePrecision.YEAR, dt.datetime(2019, 1, 1, 12, 0, tzinfo=dt.UTC)),
    ],
)
def test_dates_are_normalized_to_noon_of_the_period(
    precision: DatePrecision, expected: dt.datetime
) -> None:
    """Midnight would shift the date for every reader west of Greenwich; noon never does."""
    moment = dt.datetime(2019, 3, 12, 22, 30, tzinfo=dt.UTC)
    assert services.normalize_occurred_at(moment, precision) == expected


def test_unknown_keys_raise(user: User) -> None:
    with acting_as(user), pytest.raises(UnknownEventType):
        services.record("documents.invented", a_document(user))


def test_a_bad_payload_is_the_calling_apps_bug(user: User) -> None:
    event_type = registry.get(DOCUMENT_UPLOADED)
    with acting_as(user):
        document = a_document(user)
        data = event_type.describe(document)
        data.payload = {"mime_type": "application/pdf"}  # `size` missing
        with pytest.raises(ValidationError, match="does not match"):
            services._payload_dict(event_type, data.payload)


def test_a_span_that_ends_before_it_starts_is_refused(user: User) -> None:
    class Reversed(EventType[Document]):
        key = "documents.reversed"
        kind = EventKind.REAL_WORLD
        model = "documents.Document"
        label = "Reversed"

        def describe(self, obj: Document) -> EventData:
            return EventData(
                occurred_at=dt.datetime(2020, 5, 1, tzinfo=dt.UTC),
                occurred_until=dt.datetime(2019, 5, 1, tzinfo=dt.UTC),
                title="backwards",
            )

    registry.register(Reversed)
    try:
        with acting_as(user), pytest.raises(ValidationError, match="occurred_until"):
            services.record("documents.reversed", a_document(user))
    finally:
        del registry._types["documents.reversed"]


def test_keys_are_a_namespace() -> None:
    own = EventTypeRegistry()

    class First(EventType[Document]):
        key = "documents.taken"
        kind = EventKind.TECHNICAL
        model = "documents.Document"
        label = "First"

        def describe(self, obj: Document) -> EventData:
            return EventData(occurred_at=obj.created, title="x")

    class Second(First):
        pass

    class Malformed(First):
        key = "NotAKey"

    own.register(First)
    with pytest.raises(InvalidEventType, match="both claim"):
        own.register(Second)
    with pytest.raises(InvalidEventType, match="app_label"):
        own.register(Malformed)
    assert own.for_model(Document) == [own.get("documents.taken")]


def test_rebuild_is_idempotent_and_retires_what_left_backfill(user: User) -> None:
    with acting_as(user):
        kept = a_document(user, "kept.pdf")
        gone = a_document(user, "gone.pdf")
        TimelineEvent.all_objects.all().hard_delete()  # a database that predates the app

        assert services.rebuild()[DOCUMENT_UPLOADED] == (2, 0)
        assert services.rebuild()[DOCUMENT_UPLOADED] == (2, 0)
        assert TimelineEvent.objects.count() == 2

        gone.soft_delete()  # out of `backfill()`, but its event is still there
        assert services.rebuild()[DOCUMENT_UPLOADED] == (1, 1)
        assert_event(kept, DOCUMENT_UPLOADED)
        assert_no_event(gone)


def test_one_feed_per_tenant(user: User, other_user: User) -> None:
    with acting_as(user):
        a_document(user, "mine.pdf")
    with acting_as(other_user):
        assert TimelineEvent.objects.count() == 0
        a_document(other_user, "theirs.pdf")
        assert [e.title for e in TimelineEvent.objects.all()] == ["theirs.pdf"]


def test_events_survive_a_source_that_no_module_knows(user: User) -> None:
    """The reference is a label and a UUID, not a foreign key: an event about a row that is
    gone still renders — the client shows a generic, unclickable card."""
    with acting_as(user):
        event = TimelineEvent.create(
            operation=None,
            sources=[],
            owner=user,
            event_type="ghosts.appeared",
            kind=EventKind.REAL_WORLD,
            occurred_at=dt.datetime(1943, 5, 1, 12, tzinfo=dt.UTC),
            date_precision=DatePrecision.MONTH,
            title="Something happened",
            source_model="ghosts.ghost",
            source_id=uuid.uuid7(),
        )
        assert TimelineEvent.objects.get(pk=event.pk).title == "Something happened"
