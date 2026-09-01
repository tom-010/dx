import uuid
from typing import NewType

from django.db import models

from apps.core.history import tracked
from apps.core.models import BaseModel, owned_upload_path

MediaItemId = NewType("MediaItemId", uuid.UUID)


class MediaKind(models.TextChoices):
    IMAGE = "image"
    VIDEO = "video"


@tracked
class MediaItem(BaseModel):
    """An uploaded image or video, shown inline in the gallery."""

    # Default storage = the object store; `file.url` is the signed Django-served link. Keys:
    # gallery/<owner id>/<year>/<month>/<name> (apps/core/models.py::owned_upload_path).
    file = models.FileField(upload_to=owned_upload_path)
    kind = models.CharField(max_length=5, choices=MediaKind.choices)
    name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField()

    class Meta(BaseModel.Meta):
        pass
