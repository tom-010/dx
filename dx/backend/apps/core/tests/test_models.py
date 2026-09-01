"""Model bases (apps/core/models.py): UUIDv7 keys, ownership queryset, payload helpers."""

import uuid

import pytest

from apps.accounts.models import User
from apps.core.testing import acting_as
from apps.datasets.api import DatasetIn, DatasetPatch, create_dataset_for
from apps.datasets.models import Dataset, DatasetOptions

pytestmark = pytest.mark.django_db


def test_primary_keys_are_time_ordered_uuid7(user: User) -> None:
    with acting_as(user):
        first = create_dataset_for(user, name="first")
        second = create_dataset_for(user, name="second")

        assert isinstance(first.pk, uuid.UUID)
        assert first.pk.version == 7
        assert first.pk < second.pk  # created later → sorts later, like an auto-increment id
        assert Dataset.objects.get(pk=str(first.pk)) == first  # string form works for lookups


def test_for_user_scopes_owned_models(user: User, other_user: User) -> None:
    with acting_as(user):
        mine = create_dataset_for(user, name="mine")
    with acting_as(other_user):
        create_dataset_for(other_user, name="theirs")

    with acting_as(user):
        assert list(Dataset.objects.for_user(user)) == [mine]
        assert user.datasets.get() == mine  # reverse accessor from BaseModel.owner


def test_payload_helpers_keep_typed_json_values(user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="csv")

        # `tags` is a schema field but not a column of this row (it lives in DatasetTag), so the
        # payload helpers are told to skip it — the router sets the tags separately.
        dataset.set_payload(
            DatasetIn(name="tsv", options=DatasetOptions(delimiter="\t")), exclude={"tags"}
        )
        assert isinstance(dataset.options, DatasetOptions)  # not model_dump()ed into a dict
        assert dataset.options.delimiter == "\t"
        assert dataset.row_count == 0  # PUT: omitted fields take the schema default

        dataset.set_payload_partial(DatasetPatch(row_count=3), exclude={"tags"})
        assert (dataset.name, dataset.row_count) == ("tsv", 3)  # PATCH: only what was sent

        dataset.save()
        dataset.refresh_from_db()
        assert dataset.options == DatasetOptions(delimiter="\t")
