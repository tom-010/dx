"""The feed over HTTP: filters, the registry dump, deep links, and tenant isolation."""

import datetime as dt
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.core.testing import acting_as
from apps.documents.timeline_events import DOCUMENT_UPLOADED
from apps.timeline.models import DatePrecision, EventKind, EventStatus, TimelineEvent

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def media_root(settings: Settings, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = tmp_path


def upload(auth_client: Client, name: str = "scan.pdf") -> dict[str, Any]:
    # Distinct bytes per name — the library files one document per distinct content.
    file = SimpleUploadedFile(name, f"%PDF-1.4 {name}".encode(), content_type="application/pdf")
    response = auth_client.post("/api/documents/upload", {"files": [file]})
    assert response.status_code == 201, response.content
    document: dict[str, Any] = response.json()[0]
    return document


def test_an_upload_shows_up_in_the_feed(auth_client: Client) -> None:
    document = upload(auth_client)

    listed = auth_client.get("/api/timeline")
    assert listed.status_code == 200
    body = listed.json()
    assert body["count"] == 1
    event = body["items"][0]
    assert event["event_type"] == DOCUMENT_UPLOADED
    assert event["kind"] == "technical"
    assert event["title"] == "scan.pdf"
    # The backend ships a reference, never a route: the SPA registry turns this into a link.
    assert event["source"] == {"type": "documents.document", "id": document["id"]}
    assert event["payload"]["mime_type"] == "application/pdf"


def test_renaming_a_document_renames_its_card(auth_client: Client) -> None:
    document = upload(auth_client)

    patched = auth_client.patch(
        f"/api/documents/{document['id']}",
        {"title": "Arztbrief Dr. Müller"},
        content_type="application/json",
    )
    assert patched.status_code == 200

    body = auth_client.get("/api/timeline").json()
    assert body["count"] == 1  # updated, not appended
    assert body["items"][0]["title"] == "Arztbrief Dr. Müller"


def test_deleting_a_document_takes_its_card_with_it(auth_client: Client) -> None:
    document = upload(auth_client)
    assert auth_client.delete(f"/api/documents/{document['id']}").status_code == 204

    assert auth_client.get("/api/timeline").json() == {"items": [], "count": 0}


def test_filters(auth_client: Client, user: User) -> None:
    upload(auth_client, "a.pdf")
    with acting_as(user):
        TimelineEvent.create(
            operation=None,
            sources=[],
            owner=user,
            event_type="ghosts.appeared",
            kind=EventKind.REAL_WORLD,
            status=EventStatus.SUGGESTED,
            occurred_at=dt.datetime(1943, 5, 1, 12, tzinfo=dt.UTC),
            date_precision=DatePrecision.MONTH,
            title="Something happened",
            source_model="ghosts.ghost",
            source_id=uuid.uuid7(),
        )

    def titles(query: str) -> list[str]:
        body = auth_client.get(f"/api/timeline{query}").json()
        return [item["title"] for item in body["items"]]

    # Only active events by default — a suggestion nobody confirmed is not the record yet.
    assert titles("") == ["a.pdf"]
    assert titles("?status=active,suggested") == ["a.pdf", "Something happened"]  # newest first
    assert titles("?status=suggested") == ["Something happened"]
    assert titles("?kind=real_world&status=suggested") == ["Something happened"]
    assert titles(f"?types={DOCUMENT_UPLOADED}") == ["a.pdf"]
    assert titles("?status=active,suggested&until=1950-01-01T00:00:00Z") == ["Something happened"]
    assert titles("?status=active,suggested&since=1950-01-01T00:00:00Z") == ["a.pdf"]


def test_event_types_are_the_filter_uis_vocabulary(auth_client: Client) -> None:
    listed = auth_client.get("/api/timeline/event-types")
    assert listed.status_code == 200
    keys = {t["key"]: t for t in listed.json()}
    assert keys[DOCUMENT_UPLOADED]["label"] == "Document uploaded"
    assert keys[DOCUMENT_UPLOADED]["kind"] == "technical"


def test_one_event_by_id_for_a_deep_link(auth_client: Client) -> None:
    upload(auth_client)
    event_id = auth_client.get("/api/timeline").json()["items"][0]["id"]

    fetched = auth_client.get(f"/api/timeline/{event_id}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "scan.pdf"

    assert auth_client.get(f"/api/timeline/{uuid.uuid7()}").status_code == 404


def test_another_users_feed_is_not_visible(
    auth_client: Client, other_user: User, client_for: Callable[[User], Client]
) -> None:
    upload(auth_client)
    event_id = auth_client.get("/api/timeline").json()["items"][0]["id"]
    other = client_for(other_user)

    assert other.get("/api/timeline").json() == {"items": [], "count": 0}
    assert other.get(f"/api/timeline/{event_id}").status_code == 404


def test_the_feed_needs_a_token(client: Client) -> None:
    assert client.get("/api/timeline").status_code == 401
