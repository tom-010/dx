import uuid
from typing import NewType

from django.db import models

from apps.core.models import OwnedModel

DocumentId = NewType("DocumentId", uuid.UUID)


class Document(OwnedModel):
    """An uploaded file plus the metadata we show in listings."""

    # Storage backend is Django's default storage: the S3-compatible object store (settings).
    file = models.FileField(upload_to="documents/%Y/%m/")
    name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True)
    size = models.PositiveBigIntegerField()

    class Meta(OwnedModel.Meta):
        pass
