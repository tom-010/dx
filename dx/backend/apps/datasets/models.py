import uuid
from typing import NewType

from django.db import models
from django_pydantic_field import SchemaField
from pydantic import BaseModel as PydanticModel
from pydantic import ConfigDict, Field

from apps.core.history import tracked
from apps.core.models import OwnedModel

DatasetId = NewType("DatasetId", uuid.UUID)


class DatasetOptions(PydanticModel):
    """Import settings of a dataset — a typed JSON column (django-pydantic-field, NOTES.md §5).

    Stored as `jsonb`, validated on load and save, and part of the API contract (nested object
    in `DatasetOut`/`DatasetIn`, so the frontend gets a typed `DatasetOptions` as well).
    Evolve it like any pydantic model: new fields need a default so old rows still load.
    """

    model_config = ConfigDict(extra="forbid")

    delimiter: str = Field(default=",", min_length=1, max_length=1)
    has_header: bool = True
    encoding: str = "utf-8"


@tracked
class Dataset(OwnedModel):
    """Demo entity: a named collection of rows managed by the app."""

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    row_count = models.PositiveIntegerField(default=0)
    options = SchemaField(DatasetOptions, default=DatasetOptions)
    # Never a bare ManyToManyField: the auto-created join table is not a model, so it can be
    # neither owned nor versioned, and a tag change would leave no version row behind it
    # (system check tenant.E002, and `test_history.py::test_no_implicit_m2m_on_versioned_models`).
    # Read tags through `dataset.tag_links`, not through this descriptor: Django's m2m manager
    # queries the join table directly and would happily return soft-deleted links.
    tags: models.ManyToManyField[Tag, DatasetTag] = models.ManyToManyField(
        "Tag", through="DatasetTag", related_name="datasets"
    )

    class Meta(OwnedModel.Meta):
        pass

    def tag_names(self) -> list[str]:
        """This dataset's tags, sorted. Reads `tag_links` — the owned related manager, which
        leaves out soft-deleted links — so never `self.tags`."""
        return sorted((link.tag.name for link in self.tag_links.all()), key=str.casefold)


@tracked
class Tag(OwnedModel):
    """A label the user puts on datasets. Exists only while something is tagged with it
    (`api.prune_unused_tags`), so the list stays the list of tags actually in use."""

    name = models.CharField(max_length=100)

    class Meta(OwnedModel.Meta):
        constraints = [
            # Conditional: a soft-deleted tag must not reserve its name forever, or "sales"
            # could never be used again once it fell out of use.
            models.UniqueConstraint(
                fields=["owner", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="uniq_active_tag_name_per_owner",
            )
        ]


@tracked
class DatasetTag(OwnedModel):
    """The join between a dataset and a tag — an owned, versioned model of its own.

    `on_delete=CASCADE`, not `PROTECT`: nothing hard-deletes these except tenant erasure
    (`apps/core/tenants.py`), which walks the tables in name order and would hit
    `datasets_dataset` before `datasets_datasettag`. PROTECT would turn the one legitimate
    hard delete into a `ProtectedError`; the protection that matters is the trigger.
    """

    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="tag_links")
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="dataset_links")

    class Meta(OwnedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["dataset", "tag"],
                condition=models.Q(deleted_at__isnull=True),
                name="uniq_active_dataset_tag",
            )
        ]

    def __str__(self) -> str:
        return f"{self.dataset} · {self.tag}"
