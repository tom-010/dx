"""Business logic for datasets. Plain typed Python; no request objects, no framework glue.

Every function takes the acting `user`: reads go through `Dataset.objects.for_user(user)`, so a
dataset of another user does not exist from the caller's point of view (`DatasetNotFound`).
"""

from django.db.models import QuerySet

from apps.accounts.models import User
from apps.datasets.models import Dataset, DatasetId, DatasetOptions
from apps.datasets.schemas import DatasetIn, DatasetPatch


class DatasetNotFound(Exception):
    pass


def list_datasets(user: User) -> QuerySet[Dataset]:
    """The user's datasets, newest first. A queryset so the API can paginate it (count + slice)."""
    return Dataset.objects.for_user(user)


def get_dataset(user: User, dataset_id: DatasetId) -> Dataset:
    try:
        return Dataset.objects.for_user(user).get(pk=dataset_id)
    except Dataset.DoesNotExist as exc:
        raise DatasetNotFound(dataset_id) from exc


def create_dataset(
    user: User,
    *,
    name: str,
    description: str = "",
    row_count: int = 0,
    options: DatasetOptions | None = None,
) -> Dataset:
    return Dataset.objects.create(
        owner=user,
        name=name,
        description=description,
        row_count=row_count,
        options=options or DatasetOptions(),
    )


def update_dataset(user: User, dataset_id: DatasetId, payload: DatasetIn) -> Dataset:
    """PUT: replace every field with the payload (schema defaults for omitted ones)."""
    dataset = get_dataset(user, dataset_id)
    dataset.set_payload(payload)
    dataset.save()
    return dataset


def patch_dataset(user: User, dataset_id: DatasetId, payload: DatasetPatch) -> Dataset:
    """PATCH: change only the fields the client sent."""
    dataset = get_dataset(user, dataset_id)
    dataset.set_payload_partial(payload)
    dataset.save()
    return dataset


def delete_dataset(user: User, dataset_id: DatasetId) -> None:
    deleted, _ = Dataset.objects.for_user(user).filter(pk=dataset_id).delete()
    if deleted == 0:
        raise DatasetNotFound(dataset_id)
