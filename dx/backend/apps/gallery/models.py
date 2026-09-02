import uuid
from typing import NewType

from django.core.files.base import ContentFile
from django.db import models

from apps.core.history import tracked
from apps.core.models import OwnedModel, owned_upload_path

MediaItemId = NewType("MediaItemId", uuid.UUID)


class MediaKind(models.TextChoices):
    IMAGE = "image"
    VIDEO = "video"


@tracked
class MediaItem(OwnedModel):
    """An uploaded image or video, shown inline in the gallery."""

    # Default storage = the object store; `file.url` is the signed Django-served link. Keys:
    # gallery/<owner id>/<year>/<month>/<name> (apps/core/models.py::owned_upload_path).
    file = models.FileField(upload_to=owned_upload_path)
    kind = models.CharField(max_length=5, choices=MediaKind.choices)
    name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField()

    class Meta(OwnedModel.Meta):
        pass

    @staticmethod
    def example() -> MediaItem:
        # Not a real image: the model stores the bytes it is given and nothing here inspects
        # them — what an upload is allowed to contain is `apps/gallery/api.py`.
        content = b"\x89PNG\r\n\x1a\n example"
        return MediaItem(
            file=ContentFile(content, name="photo.png"),
            kind=MediaKind.IMAGE,
            name="photo.png",
            content_type="image/png",
            size=len(content),
        )
