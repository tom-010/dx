import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.gallery import services
from apps.gallery.models import MediaItem, MediaItemId, MediaKind

pytestmark = pytest.mark.django_db

PNG = b"\x89PNG\r\n\x1a\n demo"
MP4 = b"\x00\x00\x00\x18ftypmp42 demo"


@pytest.fixture(autouse=True)
def media_root(settings: Settings, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def _image(name: str = "photo.png") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, PNG, content_type="image/png")


def _video(name: str = "clip.mp4") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, MP4, content_type="video/mp4")


def test_upload_images_and_videos(auth_client: Client, client: Client, media_root: Path) -> None:
    response = auth_client.post("/api/gallery/upload", {"files": [_image(), _video()]})

    assert response.status_code == 201
    image, video = response.json()
    assert [image["name"], image["kind"], image["content_type"]] == [
        "photo.png",
        "image",
        "image/png",
    ]
    assert [video["name"], video["kind"], video["content_type"]] == [
        "clip.mp4",
        "video",
        "video/mp4",
    ]
    assert image["size"] == len(PNG)
    assert image["url"].startswith("/media/gallery/") and "?sig=" in image["url"]
    assert len(list(media_root.rglob("*.png"))) == 1

    # The link works without a bearer header, as an <img src> would use it.
    served = client.get(image["url"])
    assert served.status_code == 200
    assert served["Content-Type"] == "image/png"
    assert b"".join(served.streaming_content) == PNG  # type: ignore[attr-defined]

    listed = auth_client.get("/api/gallery")
    assert listed.status_code == 200
    assert listed.json()["count"] == 2
    assert [item["name"] for item in listed.json()["items"]] == ["clip.mp4", "photo.png"]


def test_upload_rejects_non_media(auth_client: Client) -> None:
    pdf = SimpleUploadedFile("report.pdf", b"%PDF-1.4", content_type="application/pdf")

    response = auth_client.post("/api/gallery/upload", {"files": [_image(), pdf]})

    assert response.status_code == 422
    assert response.json() == {"detail": "report.pdf is not an image or video"}
    assert MediaItem.objects.count() == 0  # the whole batch is rejected


def test_upload_rejects_empty_file(auth_client: Client) -> None:
    empty = SimpleUploadedFile("empty.png", b"", content_type="image/png")

    response = auth_client.post("/api/gallery/upload", {"files": [empty]})

    assert response.status_code == 422
    assert response.json() == {"detail": "empty.png is empty"}


def test_items_are_private_to_their_owner(
    user: User, other_user: User, client_for: Callable[[User], Client]
) -> None:
    (item,) = services.store_media_items(user, [_image()])

    bob = client_for(other_user)
    assert bob.get("/api/gallery").json()["items"] == []
    assert bob.get(f"/api/gallery/{item.pk}").status_code == 404
    assert bob.delete(f"/api/gallery/{item.pk}").status_code == 404
    assert MediaItem.objects.count() == 1


def test_delete_removes_file_and_row(auth_client: Client, user: User, media_root: Path) -> None:
    (item,) = services.store_media_items(user, [_video()])
    assert len(list(media_root.rglob("*.mp4"))) == 1

    assert auth_client.delete(f"/api/gallery/{item.pk}").status_code == 204
    assert auth_client.get(f"/api/gallery/{item.pk}").status_code == 404
    assert len(list(media_root.rglob("*.mp4"))) == 0


def test_service_guesses_type_from_name_when_browser_sends_none() -> None:
    file = SimpleUploadedFile("holiday.webm", MP4, content_type="application/octet-stream")

    assert services.media_type_of(file) == "video/webm"
    assert services.kind_of(services.media_type_of(file)) == MediaKind.VIDEO


def test_service_enforces_size_limit_per_kind() -> None:
    big_image = _image("huge.png")
    big_image.size = services.MAX_SIZE[MediaKind.IMAGE] + 1

    with pytest.raises(services.InvalidMedia, match="exceeds 25 MiB for images"):
        services.validate_upload([big_image])


def test_service_raises_for_unknown_id(user: User) -> None:
    with pytest.raises(services.MediaItemNotFound):
        services.get_media_item(user, MediaItemId(uuid.uuid4()))
