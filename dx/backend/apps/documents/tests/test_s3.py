"""Round trip through the real S3 backend (compose `s3`, RustFS). Marker `slow`: deselected by
default via the marker convention; skipped when the object store is not reachable.

    uv run pytest -m slow apps/documents/tests/test_s3.py
"""

import socket
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import pytest
from django.core.files.storage import storages
from django.core.files.uploadedfile import SimpleUploadedFile
from pytest_django.fixtures import Settings
from storages.backends.s3 import S3Storage

from apps.accounts.models import User
from apps.core.storage import ensure_bucket
from apps.core.testing import acting_as
from apps.documents.api import get_document_for, store_documents
from apps.documents.models import Document, DocumentId
from config.settings import S3_STORAGE

if TYPE_CHECKING:
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef

pytestmark = [pytest.mark.slow, pytest.mark.django_db]


def _reachable(endpoint: str) -> bool:
    url = urlparse(endpoint)
    try:
        with socket.create_connection((url.hostname or "localhost", url.port or 80), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture
def s3(settings: Settings) -> Iterator[S3Storage]:
    """Default storage = the S3 backend against a throwaway bucket; emptied afterwards."""
    endpoint = S3_STORAGE["OPTIONS"]["endpoint_url"] or "https://s3.amazonaws.com"
    if not _reachable(endpoint):
        pytest.skip(f"object store not reachable at {endpoint} (./scripts/db.sh)")
    bucket = f"test-{uuid.uuid4().hex[:12]}"
    options = {**S3_STORAGE["OPTIONS"], "bucket_name": bucket}
    # Django's setting_changed receiver drops the cached backends, so FileFields follow along.
    settings.STORAGES = {**settings.STORAGES, "default": {**S3_STORAGE, "OPTIONS": options}}
    storage = storages["default"]
    assert isinstance(storage, S3Storage)
    ensure_bucket(storage.connection.meta.client, bucket)
    yield storage
    client = storage.connection.meta.client
    versions = client.list_object_versions(Bucket=bucket)
    objects: list[ObjectIdentifierTypeDef] = [
        {"Key": v["Key"], "VersionId": v["VersionId"]} for v in versions.get("Versions", [])
    ]
    objects.extend(
        {"Key": m["Key"], "VersionId": m["VersionId"]} for m in versions.get("DeleteMarkers", [])
    )
    if objects:
        client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
    client.delete_bucket(Bucket=bucket)


def _keys(storage: S3Storage) -> list[str]:
    return sorted(obj.key for obj in storage.bucket.objects.all())


def test_upload_download_delete_through_object_store(s3: S3Storage, user: User) -> None:
    with acting_as(user):
        one, two = store_documents(
            user,
            [
                SimpleUploadedFile("a.txt", b"hello", content_type="text/plain"),
                SimpleUploadedFile("a.txt", b"hello again", content_type="text/plain"),
            ],
        )

    # Links point at Django, never at the store.
    assert one.file.url.startswith(f"/media/{one.file.name}?sig=")
    # Same upload name twice: the store must not overwrite, django-storages disambiguates.
    keys = [str(one.file.name), str(two.file.name)]
    assert keys[0] != keys[1]
    assert keys[0].startswith("documents/")
    assert _keys(s3) == sorted(keys)
    with two.file.open("rb") as stream:
        assert stream.read() == b"hello again"

    # Deleting a document is soft, so the objects stay: an earlier version of the row still
    # points at them. Only erasing the tenant removes them (apps/core/tenants.py).
    with acting_as(user):
        get_document_for(user, DocumentId(one.pk)).soft_delete()
        get_document_for(user, DocumentId(two.pk)).soft_delete()
        assert Document.objects.count() == 0
    assert _keys(s3) == sorted(keys)

    for key in keys:
        s3.delete(key)
    assert _keys(s3) == []
    # Bucket versioning: deletes only wrote delete markers; the bytes are still recoverable.
    versions = s3.connection.meta.client.list_object_versions(Bucket=s3.bucket_name)
    assert sorted(v["Key"] for v in versions.get("Versions", [])) == sorted(keys)
    assert len(versions.get("DeleteMarkers", [])) == 2
