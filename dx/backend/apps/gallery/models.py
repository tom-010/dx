import uuid
from typing import NewType

from django.db import models

from apps.core.models import OwnedModel

MediaItemId = NewType("MediaItemId", uuid.UUID)


class MediaKind(models.TextChoices):
    IMAGE = "image"
    VIDEO = "video"


class MediaItem(OwnedModel):
    """An uploaded image or video, shown inline in the gallery."""

    # Default storage = the object store; `file.url` is the signed Django-served link.
    file = models.FileField(upload_to="gallery/%Y/%m/")
    kind = models.CharField(max_length=5, choices=MediaKind.choices)
    name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField()

    class Meta(OwnedModel.Meta):
        pass
