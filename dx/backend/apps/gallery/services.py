"""Business logic for the gallery. Plain typed Python; no request objects, no framework glue.

Media items are owned: every function takes the acting `user` and reads through
`MediaItem.objects.for_user(user)`, so other users' items look like missing ones (404).
"""

import mimetypes
from collections.abc import Sequence

from django.core.files.uploadedfile import UploadedFile
from django.db.models import QuerySet

from apps.accounts.models import User
from apps.gallery.models import MediaItem, MediaItemId, MediaKind

MAX_SIZE = {MediaKind.IMAGE: 25 * 1024 * 1024, MediaKind.VIDEO: 500 * 1024 * 1024}
MAX_ITEMS_PER_UPLOAD = 20


class MediaItemNotFound(Exception):
    pass


class InvalidMedia(Exception):
    """The upload was rejected; the message is safe to show to the user."""


def list_media_items(user: User) -> QuerySet[MediaItem]:
    return MediaItem.objects.for_user(user)


def get_media_item(user: User, media_item_id: MediaItemId) -> MediaItem:
    try:
        return MediaItem.objects.for_user(user).get(pk=media_item_id)
    except MediaItem.DoesNotExist as exc:
        raise MediaItemNotFound(media_item_id) from exc


def media_type_of(file: UploadedFile[bytes]) -> str:
    """The MIME type to trust: the browser's, or a guess from the name when it sent nothing
    useful (e.g. `application/octet-stream` for unusual extensions)."""
    declared = (file.content_type or "").lower()
    if declared.startswith(("image/", "video/")):
        return declared
    guessed, _ = mimetypes.guess_type(file.name or "")
    return (guessed or declared).lower()


def kind_of(content_type: str) -> MediaKind | None:
    if content_type.startswith("image/"):
        return MediaKind.IMAGE
    if content_type.startswith("video/"):
        return MediaKind.VIDEO
    return None


def validate_upload(files: Sequence[UploadedFile[bytes]]) -> None:
    if not files:
        raise InvalidMedia("No files were uploaded")
    if len(files) > MAX_ITEMS_PER_UPLOAD:
        raise InvalidMedia(f"At most {MAX_ITEMS_PER_UPLOAD} files per upload")
    for file in files:
        if not file.name:
            raise InvalidMedia("A file has no name")
        kind = kind_of(media_type_of(file))
        if kind is None:
            raise InvalidMedia(f"{file.name} is not an image or video")
        size = file.size or 0
        if size == 0:
            raise InvalidMedia(f"{file.name} is empty")
        if size > MAX_SIZE[kind]:
            raise InvalidMedia(
                f"{file.name} exceeds {MAX_SIZE[kind] // (1024 * 1024)} MiB for {kind.value}s"
            )


def store_media_items(user: User, files: Sequence[UploadedFile[bytes]]) -> list[MediaItem]:
    """Validate the whole batch first, then persist it (all or nothing from the user's view)."""
    validate_upload(files)
    items = []
    for file in files:
        content_type = media_type_of(file)
        kind = kind_of(content_type)
        assert kind is not None  # validate_upload() guarantees it
        items.append(
            MediaItem.objects.create(
                owner=user,
                file=file,
                kind=kind,
                name=file.name or "",
                content_type=content_type,
                size=file.size or 0,
            )
        )
    return items


def delete_media_item(user: User, media_item_id: MediaItemId) -> None:
    item = get_media_item(user, media_item_id)
    item.file.delete(save=False)
    item.delete()
