"""Object-store provisioning for the configured storages (settings.STORAGES).

Plain functions over a boto3 S3 client so they can be unit-tested with a fake; the
`ensure_bucket` management command wires them to the configured storages (media + backups).
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError
from django.core.files.storage import Storage, storages
from storages.backends.s3 import S3Storage

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


@dataclass(frozen=True)
class BucketStatus:
    bucket: str
    created: bool
    versioning_enabled: bool  # True if this call turned versioning on


def s3_storage(alias: str = "default") -> S3Storage | None:
    """The configured storage `alias` if it is the S3 backend, else None (local disk)."""
    storage: Storage = storages[alias]
    return storage if isinstance(storage, S3Storage) else None


def bucket_exists(client: S3Client, bucket: str) -> bool:
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchBucket", "NotFound"}:
            return False
        raise
    return True


def ensure_bucket(client: S3Client, bucket: str) -> BucketStatus:
    """Create `bucket` if missing and make sure versioning is on. Idempotent."""
    created = not bucket_exists(client, bucket)
    if created:
        client.create_bucket(Bucket=bucket)
    versioning = client.get_bucket_versioning(Bucket=bucket).get("Status")
    enable = versioning != "Enabled"
    if enable:
        client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    return BucketStatus(bucket=bucket, created=created, versioning_enabled=enable)
