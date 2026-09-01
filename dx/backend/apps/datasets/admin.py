"""Admin pages for the datasets app.

`OwnedModelAdmin` (apps/core/admin.py) is what makes these safe: soft-delete instead of the
hard-delete button the database rejects, `all_objects` so deleted rows stay restorable, and the
tenant scoping every admin queryset goes through. Registering a plain `ModelAdmin` here would
undo all three — `apps/core/tests/test_admin.py` fails if anyone does.
"""

from django.contrib import admin

from apps.core.admin import OwnedModelAdmin
from apps.datasets.models import Dataset, DatasetTag, Tag


@admin.register(Dataset)
class DatasetAdmin(OwnedModelAdmin[Dataset]):
    list_display = ["name", "row_count", "version", "created", "deleted_at"]
    list_filter = ["deleted_at"]
    search_fields = ["name", "description"]


@admin.register(Tag)
class TagAdmin(OwnedModelAdmin[Tag]):
    list_display = ["name", "version", "created", "deleted_at"]
    list_filter = ["deleted_at"]
    search_fields = ["name"]


@admin.register(DatasetTag)
class DatasetTagAdmin(OwnedModelAdmin[DatasetTag]):
    """The explicit through model: an owned, versioned join row, so it has a history of its own
    and shows up as a child row on the dataset's revision page."""

    list_display = ["dataset", "tag", "version", "created", "deleted_at"]
    list_filter = ["deleted_at"]
