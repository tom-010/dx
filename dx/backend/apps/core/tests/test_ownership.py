"""Every owned resource is invisible to other users: listing shows nothing, and get/update/
delete answer 404 (never 403 — no information about what exists).

Register every app that exposes an `OwnedModel` in RESOURCES; the tests are generated.
"""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.core.testing import acting_as
from apps.datasets.api import create_dataset_for
from apps.documents.api import store_documents
from apps.gallery.api import store_media_items
from apps.notes.api import create_note_for


@dataclass(frozen=True)
class OwnedResource:
    name: str
    create: Callable[[User], uuid.UUID]  # creates one object for the user, returns its id
    collection: str  # list endpoint (paginated)
    item: str  # item endpoint with `{id}`
    # Methods on `item` besides GET/DELETE that must also answer 404 for foreign objects.
    updates: dict[str, dict[str, object]] = field(default_factory=dict)


def _document(user: User) -> uuid.UUID:
    file = SimpleUploadedFile("a.pdf", b"%PDF-1.4 demo", content_type="application/pdf")
    return store_documents(user, [file])[0].pk


def _media_item(user: User) -> uuid.UUID:
    file = SimpleUploadedFile("a.png", b"\x89PNG demo", content_type="image/png")
    return store_media_items(user, [file])[0].pk


RESOURCES = [
    OwnedResource(
        "datasets",
        lambda user: create_dataset_for(user, name="A's dataset").pk,
        "/api/datasets",
        "/api/datasets/{id}",
        updates={"PUT": {"name": "hijacked"}, "PATCH": {"name": "hijacked"}},
    ),
    OwnedResource("documents", _document, "/api/documents", "/api/documents/{id}"),
    OwnedResource("gallery", _media_item, "/api/gallery", "/api/gallery/{id}"),
    OwnedResource(
        "notes",
        lambda user: create_note_for(user, title="A's note").pk,
        "/api/notes",
        "/api/notes/{id}",
        updates={"PUT": {"title": "hijacked"}, "PATCH": {"title": "hijacked"}},
    ),
]


@pytest.fixture(autouse=True)
def media_root(settings: Settings, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


@pytest.mark.django_db
@pytest.mark.parametrize("resource", RESOURCES, ids=lambda r: r.name)
def test_other_users_cannot_see_or_touch_it(
    resource: OwnedResource,
    user: User,
    other_user: User,
    auth_client: Client,
    client_for: Callable[[User], Client],
) -> None:
    with acting_as(user):
        object_id = resource.create(user)
    item = resource.item.format(id=object_id)
    other = client_for(other_user)

    # The owner sees it.
    assert auth_client.get(resource.collection).json()["count"] == 1
    assert auth_client.get(item).status_code == 200

    # Another user: nothing in the list, 404 everywhere else.
    assert other.get(resource.collection).json() == {"items": [], "count": 0}
    assert other.get(item).status_code == 404
    for method, payload in resource.updates.items():
        response = other.generic(
            method, item, data=json.dumps(payload), content_type="application/json"
        )
        assert response.status_code == 404, f"{method} {item} answered {response.status_code}"
    assert other.delete(item).status_code == 404

    # ...and nothing happened to the owner's object.
    assert auth_client.get(item).status_code == 200
    assert auth_client.get(resource.collection).json()["count"] == 1
