"""Gallery: schemas, logic and the ninja router in one module — multipart upload of images and
videos, listing, delete.

Media items are owned: every read goes through `MediaItem.objects.for_user(user)`, so other
users' items look like missing ones (404). `MediaItemOut.url` is the signed `/media/…` link
(`config/media.py`), which the SPA puts straight into `<img src>` / `<video src>`.
"""

import mimetypes
import uuid
from collections.abc import Sequence

from django.core.files.uploadedfile import UploadedFile
from django.db.models import QuerySet
from django.http import HttpRequest
from ninja import File, ModelSchema, Router, Status
from ninja.errors import HttpError
from ninja.files import UploadedFile as NinjaUploadedFile
from ninja.pagination import PageNumberPagination, paginate

from apps.accounts.api import current_user
from apps.accounts.models import User
from apps.gallery.models import MediaItem, MediaItemId, MediaKind

router = Router(tags=["gallery"])

MAX_SIZE = {MediaKind.IMAGE: 25 * 1024 * 1024, MediaKind.VIDEO: 500 * 1024 * 1024}
MAX_ITEMS_PER_UPLOAD = 20


class MediaItemOut(ModelSchema):
    id: uuid.UUID
    # Signed, expiring link served by Django (config/media.py) — usable directly as
    # <img src> / <video src>. Re-fetch the list rather than caching it for hours.
    url: str

    class Meta:
        model = MediaItem
        fields = ["id", "kind", "name", "content_type", "size", "created"]

    @staticmethod
    def resolve_url(obj: MediaItem) -> str:
        return obj.file.url


def get_media_item_for(user: User, media_item_id: MediaItemId) -> MediaItem:
    """One media item, or a 404 — another user's item does not exist from here."""
    try:
        return MediaItem.objects.for_user(user).get(pk=media_item_id)
    except MediaItem.DoesNotExist:
        raise HttpError(404, "Media item not found") from None


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
    """Reject the whole batch before anything is stored (422; the messages are safe to show)."""
    if not files:
        raise HttpError(422, "No files were uploaded")
    if len(files) > MAX_ITEMS_PER_UPLOAD:
        raise HttpError(422, f"At most {MAX_ITEMS_PER_UPLOAD} files per upload")
    for file in files:
        if not file.name:
            raise HttpError(422, "A file has no name")
        kind = kind_of(media_type_of(file))
        if kind is None:
            raise HttpError(422, f"{file.name} is not an image or video")
        size = file.size or 0
        if size == 0:
            raise HttpError(422, f"{file.name} is empty")
        if size > MAX_SIZE[kind]:
            raise HttpError(
                422, f"{file.name} exceeds {MAX_SIZE[kind] // (1024 * 1024)} MiB for {kind.value}s"
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
            MediaItem.create(
                operation=None,
                sources=[],
                owner=user,
                file=file,
                kind=kind,
                name=file.name or "",
                content_type=content_type,
                size=file.size or 0,
            )
        )
    return items


# --- Endpoints ----------------------------------------------------------------------------------


@router.get("/gallery", response=list[MediaItemOut])
@paginate(PageNumberPagination)
def list_media_items(request: HttpRequest) -> QuerySet[MediaItem]:
    return MediaItem.objects.for_user(current_user(request))


@router.post("/gallery/upload", response={201: list[MediaItemOut]})
def upload_media_items(
    request: HttpRequest, files: File[list[NinjaUploadedFile]]
) -> Status[list[MediaItem]]:
    """Upload images and/or videos as multipart/form-data (field name `files`)."""
    return Status(201, store_media_items(current_user(request), files))


@router.get("/gallery/{media_item_id}", response=MediaItemOut)
def get_media_item(request: HttpRequest, media_item_id: uuid.UUID) -> MediaItem:
    return get_media_item_for(current_user(request), MediaItemId(media_item_id))


@router.delete("/gallery/{media_item_id}", response={204: None})
def delete_media_item(request: HttpRequest, media_item_id: uuid.UUID) -> Status[None]:
    """Soft delete; the stored object stays (see `delete_document` in apps/documents/api.py)."""
    get_media_item_for(current_user(request), MediaItemId(media_item_id)).soft_delete()
    return Status(204, None)
