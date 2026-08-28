import uuid
from typing import NewType

from django.db import models
from django_pydantic_field import SchemaField
from pydantic import BaseModel as PydanticModel
from pydantic import ConfigDict, Field

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


class Dataset(OwnedModel):
    """Demo entity: a named collection of rows managed by the app."""

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    row_count = models.PositiveIntegerField(default=0)
    options = SchemaField(DatasetOptions, default=DatasetOptions)

    class Meta(OwnedModel.Meta):
        pass
