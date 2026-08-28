"""Model bases (apps/core/models.py): UUIDv7 keys, ownership queryset, payload helpers."""

import uuid

import pytest

from apps.accounts.models import User
from apps.datasets.models import Dataset, DatasetOptions
from apps.datasets.schemas import DatasetIn, DatasetPatch
from apps.datasets.services import create_dataset

pytestmark = pytest.mark.django_db


def test_primary_keys_are_time_ordered_uuid7(user: User) -> None:
    first = create_dataset(user, name="first")
    second = create_dataset(user, name="second")

    assert isinstance(first.pk, uuid.UUID)
    assert first.pk.version == 7
    assert first.pk < second.pk  # created later → sorts later, like an auto-increment id
    assert Dataset.objects.get(pk=str(first.pk)) == first  # string form works for lookups


def test_for_user_scopes_owned_models(user: User, other_user: User) -> None:
    mine = create_dataset(user, name="mine")
    create_dataset(other_user, name="theirs")

    assert list(Dataset.objects.for_user(user)) == [mine]
    assert user.datasets.get() == mine  # reverse accessor from OwnedModel.owner


def test_payload_helpers_keep_typed_json_values(user: User) -> None:
    dataset = create_dataset(user, name="csv")

    dataset.set_payload(DatasetIn(name="tsv", options=DatasetOptions(delimiter="\t")))
    assert isinstance(dataset.options, DatasetOptions)  # not model_dump()ed into a dict
    assert dataset.options.delimiter == "\t"
    assert dataset.row_count == 0  # PUT: omitted fields take the schema default

    dataset.set_payload_partial(DatasetPatch(row_count=3))
    assert (dataset.name, dataset.row_count) == ("tsv", 3)  # PATCH: only what was sent

    dataset.save()
    dataset.refresh_from_db()
    assert dataset.options == DatasetOptions(delimiter="\t")
