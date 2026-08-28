"""Minimal stub for django-storages' S3 backend (the package ships no type hints).

Only what this project touches is declared: the settings `S3Storage(**settings)` copies onto
the instance, the boto3 handles (`connection`, `bucket`) typed through boto3-stubs so that
`storage.connection.meta.client` is an `S3Client`, and the storage API it overrides. Extend it
when more of the backend is used — never fall back to `ignore_missing_imports` (mypy runs with
`disallow_any_unimported`, see CLAUDE.md "Type checking").
"""

from typing import Any

from botocore.config import Config
from django.core.files.storage import Storage
from mypy_boto3_s3.service_resource import Bucket, S3ServiceResource

class S3Storage(Storage):
    # Settings — see `get_default_settings()` in storages/backends/s3.py; every key can be
    # passed in STORAGES[...]["OPTIONS"]. `bucket_name` is typed non-optional: the backend is
    # unusable without one, and config/settings.py always sets it.
    access_key: str | None
    secret_key: str | None
    security_token: str | None
    session_profile: str | None
    file_overwrite: bool
    object_parameters: dict[str, Any]
    bucket_name: str
    querystring_auth: bool
    querystring_expire: int
    signature_version: str | None
    location: str
    custom_domain: str | None
    addressing_style: str | None
    endpoint_url: str | None
    region_name: str | None
    use_ssl: bool
    verify: bool | str | None
    max_memory_size: int
    default_acl: str | None
    use_threads: bool
    client_config: Config
    default_content_type: str

    def __init__(self, **settings: object) -> None: ...
    @property
    def connection(self) -> S3ServiceResource: ...
    @property
    def unsigned_connection(self) -> S3ServiceResource: ...
    @property
    def bucket(self) -> Bucket: ...
    def get_object_parameters(self, name: str) -> dict[str, Any]: ...
    def url(
        self,
        name: str | None,
        parameters: dict[str, Any] | None = None,
        expire: int | None = None,
        http_method: str | None = None,
    ) -> str: ...

class S3StaticStorage(S3Storage): ...
