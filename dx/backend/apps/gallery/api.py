"""HTTP surface for the gallery: multipart upload of images/videos, listing, delete."""

import uuid

from django.db.models import QuerySet
from django.http import HttpRequest
from ninja import File, ModelSchema, Router, Status, UploadedFile
from ninja.errors import HttpError
from ninja.pagination import PageNumberPagination, paginate

from apps.accounts.auth import current_user
from apps.gallery import services
from apps.gallery.models import MediaItem, MediaItemId

router = Router(tags=["gallery"])


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


@router.get("/gallery", response=list[MediaItemOut])
@paginate(PageNumberPagination)
def list_media_items(request: HttpRequest) -> QuerySet[MediaItem]:
    return services.list_media_items(current_user(request))


@router.post("/gallery/upload", response={201: list[MediaItemOut]})
def upload_media_items(
    request: HttpRequest, files: File[list[UploadedFile]]
) -> Status[list[MediaItem]]:
    """Upload images and/or videos as multipart/form-data (field name `files`)."""
    try:
        items = services.store_media_items(current_user(request), files)
    except services.InvalidMedia as exc:
        raise HttpError(422, str(exc)) from None
    return Status(201, items)


@router.get("/gallery/{media_item_id}", response=MediaItemOut)
def get_media_item(request: HttpRequest, media_item_id: uuid.UUID) -> MediaItem:
    try:
        return services.get_media_item(current_user(request), MediaItemId(media_item_id))
    except services.MediaItemNotFound:
        raise HttpError(404, "Media item not found") from None


@router.delete("/gallery/{media_item_id}", response={204: None})
def delete_media_item(request: HttpRequest, media_item_id: uuid.UUID) -> Status[None]:
    try:
        services.delete_media_item(current_user(request), MediaItemId(media_item_id))
    except services.MediaItemNotFound:
        raise HttpError(404, "Media item not found") from None
    return Status(204, None)
