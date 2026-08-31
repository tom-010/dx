"""`/media/<key>?sig=…` — uploads served by Django from the default storage (config/media.py)."""

import uuid
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.core.testing import acting_as
from apps.documents.api import store_documents
from apps.documents.models import Document
from config.media import media_url, sign_media

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def media_root(settings: Settings, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def _upload(user: User, name: str = "pic.png", content: bytes = b"\x89PNG demo") -> Document:
    with acting_as(user):
        (document,) = store_documents(
            user, [SimpleUploadedFile(name, content, content_type="image/png")]
        )
    return document


def test_file_url_is_signed_and_served_by_django(client: Client, user: User) -> None:
    document = _upload(user)

    url = document.file.url
    assert url.startswith(f"/media/{document.file.name}?sig=")
    response = client.get(url)  # anonymous: an <img src> sends no bearer header

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"\x89PNG demo"  # type: ignore[attr-defined]
    assert response["Content-Type"] == "image/png"
    assert "attachment" not in response.get("Content-Disposition", "")
    assert "private" in response["Cache-Control"]
    assert "max-age=3600" in response["Cache-Control"]


def test_media_rejects_unsigned_and_foreign_links(client: Client, user: User) -> None:
    document = _upload(user)
    key = str(document.file.name)

    assert client.get(f"/media/{key}").status_code == 403
    assert client.get(f"/media/{key}?sig=nope").status_code == 403
    foreign = client.get(f"/media/{key}?sig={sign_media('documents/other.png')}")
    assert foreign.status_code == 403
    assert foreign.json() == {"detail": "Invalid or expired media link"}


def test_media_links_expire(client: Client, settings: Settings, user: User) -> None:
    url = _upload(user).file.url
    settings.MEDIA_LINK_MAX_AGE = -1  # everything signed before now is too old

    assert client.get(url).status_code == 403


def test_media_returns_404_for_missing_object(client: Client) -> None:
    response = client.get(media_url(f"documents/{uuid.uuid7()}/2026/08/gone.png"))

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_media_is_not_handled_by_the_spa_catch_all(client: Client, settings: Settings) -> None:
    settings.SPA_INDEX = Path("/nonexistent/index.html")  # SPA would answer 503

    assert client.get("/media/anything.png").status_code == 403
