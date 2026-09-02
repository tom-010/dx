import uuid
from typing import NewType

from django.core.files.base import ContentFile
from django.db import models

from apps.core.history import tracked
from apps.core.models import OwnedModel, owned_upload_path

DocumentId = NewType("DocumentId", uuid.UUID)


@tracked
class Document(OwnedModel):
    """An uploaded file plus the metadata we show in listings."""

    # Storage backend is Django's default storage: the S3-compatible object store (settings).
    # Keys: documents/<owner id>/<year>/<month>/<name> (apps/core/models.py::owned_upload_path).
    file = models.FileField(upload_to=owned_upload_path)
    name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True)
    size = models.PositiveBigIntegerField()

    class Meta(OwnedModel.Meta):
        pass

    @staticmethod
    def example() -> Document:
        # Saving this writes the bytes to the object store like any upload would; the key comes
        # from `owned_upload_path`, so it needs the tenant context `save_example` provides.
        content = b"name,amount\nrent,1200\n"
        return Document(
            file=ContentFile(content, name="expenses.csv"),
            name="expenses.csv",
            content_type="text/csv",
            size=len(content),
        )
