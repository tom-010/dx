import uuid
from typing import NewType

from django.db import models

from apps.core.history import tracked
from apps.core.models import BaseModel

NoteId = NewType("NoteId", uuid.UUID)


# Every write is captured in NoteEvent by a database trigger (apps/core/history.py).
# Removing @tracked means listing the model in HISTORY_EXEMPT with a reason — a test fails
# otherwise. Never use a plain ManyToManyField here: declare an owned `through=` model, or the
# join rows change with no version row behind them.
@tracked
class Note(BaseModel):
    """A piece of writing the user keeps.

    Notes are the project's showcase for versioning and lineage (see `apps/notes/api.py`):
    every edit is a version, and merging records which *version* of each source note the result
    was built from.
    """

    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    # A plain comma-separated string, deliberately: notes are a showcase, and a tag here is a
    # label on one note, not a thing in its own right. `apps/datasets` has the other shape —
    # an owned, versioned `Tag` model with a join table — for when tags need to be shared,
    # renamed or counted. Normalised on write (`api.normalize_tags`).
    tags = models.CharField(max_length=500, blank=True)

    class Meta(BaseModel.Meta):
        pass

    def tag_list(self) -> list[str]:
        return [tag for tag in (part.strip() for part in self.tags.split(",")) if tag]

    def __str__(self) -> str:
        return self.title
