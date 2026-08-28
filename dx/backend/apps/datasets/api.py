"""HTTP surface for datasets: thin ninja router delegating to services.

This module is the template for new feature modules (`manage.py startmodule <name>` copies the
same shape): paginated list, get, create, PUT, PATCH, delete — all scoped to the caller.
"""

import uuid

from django.db.models import QuerySet
from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError
from ninja.pagination import PageNumberPagination, paginate

from apps.accounts.auth import current_user
from apps.datasets import services
from apps.datasets.models import Dataset, DatasetId
from apps.datasets.schemas import DatasetIn, DatasetOut, DatasetPatch

router = Router(tags=["datasets"])


@router.get("/datasets", response=list[DatasetOut])
@paginate(PageNumberPagination)  # `?page=&page_size=` → `{items: [...], count: n}`
def list_datasets(request: HttpRequest) -> QuerySet[Dataset]:
    return services.list_datasets(current_user(request))


@router.post("/datasets", response={201: DatasetOut})
def create_dataset(request: HttpRequest, payload: DatasetIn) -> Status[Dataset]:
    dataset = services.create_dataset(
        current_user(request),
        name=payload.name,
        description=payload.description,
        row_count=payload.row_count,
        options=payload.options,
    )
    return Status(201, dataset)


@router.get("/datasets/{dataset_id}", response=DatasetOut)
def get_dataset(request: HttpRequest, dataset_id: uuid.UUID) -> Dataset:
    try:
        return services.get_dataset(current_user(request), DatasetId(dataset_id))
    except services.DatasetNotFound:
        raise HttpError(404, "Dataset not found") from None


@router.put("/datasets/{dataset_id}", response=DatasetOut)
def update_dataset(request: HttpRequest, dataset_id: uuid.UUID, payload: DatasetIn) -> Dataset:
    """Full update: every field is replaced (omitted fields take their defaults)."""
    try:
        return services.update_dataset(current_user(request), DatasetId(dataset_id), payload)
    except services.DatasetNotFound:
        raise HttpError(404, "Dataset not found") from None


@router.patch("/datasets/{dataset_id}", response=DatasetOut)
def patch_dataset(request: HttpRequest, dataset_id: uuid.UUID, payload: DatasetPatch) -> Dataset:
    """Partial update: only the fields present in the body change."""
    try:
        return services.patch_dataset(current_user(request), DatasetId(dataset_id), payload)
    except services.DatasetNotFound:
        raise HttpError(404, "Dataset not found") from None


@router.delete("/datasets/{dataset_id}", response={204: None})
def delete_dataset(request: HttpRequest, dataset_id: uuid.UUID) -> Status[None]:
    try:
        services.delete_dataset(current_user(request), DatasetId(dataset_id))
    except services.DatasetNotFound:
        raise HttpError(404, "Dataset not found") from None
    return Status(204, None)
