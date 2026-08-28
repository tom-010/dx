"""Media files: stored in the object store, served by Django.

Every `FileField.url` in the app resolves to `/media/<key>?sig=…` — a signed, expiring URL that
`serve_media` below answers by streaming the object from the default storage. Browsers can load
these with plain `<img src>` / `<a href>` (no bearer header), the store itself never needs a
public endpoint, and nothing in the frontend depends on where the bytes live. Both storage
backends (S3, local disk) share the URL scheme via `SignedMediaUrlMixin`, so dev, tests and
prod behave the same.
"""

from pathlib import PurePosixPath

from django.conf import settings
from django.core import signing
from django.core.files.storage import FileSystemStorage, storages
from django.http import FileResponse, HttpRequest, JsonResponse
from django.http.response import HttpResponseBase
from django.urls import reverse
from django.utils.cache import patch_cache_control
from storages.backends.s3 import S3Storage

_SALT = "media.url"


def sign_media(name: str) -> str:
    """Signature for the storage key `name`; valid for settings.MEDIA_LINK_MAX_AGE seconds."""
    return signing.dumps(name, salt=_SALT)


def verify_media(name: str, signature: str) -> bool:
    try:
        signed_name = signing.loads(signature, salt=_SALT, max_age=settings.MEDIA_LINK_MAX_AGE)
    except signing.BadSignature:
        return False
    return bool(signed_name == name)


def media_url(name: str) -> str:
    return f"{reverse('media', kwargs={'path': name})}?sig={sign_media(name)}"


class SignedMediaUrlMixin:
    """`storage.url(name)` → the Django-served signed URL instead of the backend's own URL
    (a presigned S3 URL or MEDIA_URL + name)."""

    def url(self, name: str | None, *args: object, **kwargs: object) -> str:
        if not name:
            raise ValueError("This file has no name")
        return media_url(name)


class S3MediaStorage(SignedMediaUrlMixin, S3Storage):
    pass


class LocalMediaStorage(SignedMediaUrlMixin, FileSystemStorage):
    pass


def serve_media(request: HttpRequest, path: str) -> HttpResponseBase:
    """Stream one stored object. Public but signed (see `media_url`); 403 without a valid link."""
    if not verify_media(path, request.GET.get("sig", "")):
        return JsonResponse({"detail": "Invalid or expired media link"}, status=403)
    try:
        stream = storages["default"].open(path, "rb")
    except FileNotFoundError:
        return JsonResponse({"detail": "Not Found"}, status=404)
    # Inline (images render, PDFs open in the browser); `<a download>` still saves it. The
    # documents API has its own endpoint for forced downloads with the original file name.
    response = FileResponse(stream, filename=PurePosixPath(path).name)
    patch_cache_control(response, private=True, max_age=settings.MEDIA_LINK_MAX_AGE)
    return response
