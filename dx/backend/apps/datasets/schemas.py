"""API schemas for datasets. Separate from `api.py` because services take the payload
objects (`DatasetIn`, `DatasetPatch`) for PUT/PATCH — services must not import the router."""

import uuid

from ninja import Field, ModelSchema

from apps.core.schemas import StrictSchema
from apps.datasets.models import Dataset, DatasetOptions


class DatasetOut(ModelSchema):
    # ModelSchema would mark these optional/nullable in the OpenAPI output (pk, blank=True,
    # default=...); redeclaring them keeps the generated TS types strict.
    id: uuid.UUID
    description: str
    row_count: int
    options: DatasetOptions

    class Meta:
        model = Dataset
        fields = ["id", "name", "description", "row_count", "options", "created", "modified"]


class DatasetIn(StrictSchema):
    """Create (POST) and full update (PUT): every field, omitted ones take the defaults."""

    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    row_count: int = Field(default=0, ge=0)
    options: DatasetOptions = Field(default_factory=DatasetOptions)


class DatasetPatch(StrictSchema):
    """Partial update (PATCH): only the fields that are present change."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    row_count: int | None = Field(default=None, ge=0)
    options: DatasetOptions | None = None
