"""Test helpers for the documents app: accept a file type the product does not offer.

Uploads are restricted to `api.SUPPORTED_UPLOAD_FORMATS` (PDF), but most tests want bytes that
are cheap to write and a type no real extractor will ever be handed — `application/x-fake`
against `FakeStrategy`, a CSV for the dataset import. `uploadable(...)` is where a test says
so, the same way it registers a strategy for the type it invents.
"""

import contextlib
from collections.abc import Iterator

from apps.documents import api


@contextlib.contextmanager
def uploadable(*mime_types: str) -> Iterator[None]:
    """Let uploads of these MIME types through for the duration of the block."""
    original = api.SUPPORTED_UPLOAD_FORMATS
    api.SUPPORTED_UPLOAD_FORMATS = [
        *original,
        *(
            api.UploadFormat(label=mime_type, mime_type=mime_type, extensions=())
            for mime_type in mime_types
        ),
    ]
    try:
        yield
    finally:
        api.SUPPORTED_UPLOAD_FORMATS = original
