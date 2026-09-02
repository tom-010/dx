"""`save_deep` (`apps/core/save_deep.py`): a tree of unsaved rows, children first.

The walk itself is inherited from django-save-deep; what is tested here is what this project
changed about it, because a write is a version and a version cannot be taken back.
"""

import pytest
from django.db import DatabaseError

from apps.accounts.models import User
from apps.core.examples import save_example
from apps.core.save_deep import save_deep
from apps.core.testing import acting_as
from apps.datasets.models import Dataset, DatasetTag, Tag

pytestmark = pytest.mark.django_db


def test_saves_the_children_before_the_row(user: User) -> None:
    with acting_as(user):
        link = save_deep(
            DatasetTag(dataset=Dataset(name="Q3"), tag=Tag(name="finance")),
            operation=None,
            sources=[],
        )

        assert Dataset.objects.for_user(user).get() == link.dataset
        assert link.dataset_id == link.dataset.pk  # the insert's pk, copied onto the row
        assert link.owner_id == user.pk  # filled in by OwnedModel.save(), row by row


def test_a_child_that_already_exists_is_not_written_again(user: User) -> None:
    """Upstream re-saves every foreign key it walks. Here that would be a second version of an
    unchanged row, with this call's operation and sources hung on it."""
    with acting_as(user):
        tag = save_example(Tag.example())

        save_deep(DatasetTag(dataset=Dataset(name="Q3"), tag=tag), operation=None, sources=[])

        tag.refresh_from_db()
        assert tag.version == 1
        assert len(tag.history()) == 1


def test_a_row_that_cannot_be_written_takes_its_children_with_it(user: User) -> None:
    """One transaction: children in the database and no row that needed them is a state nothing
    can clean up, because by then the children have a version history."""
    too_long = "x" * 200  # Tag.name is varchar(100)

    with acting_as(user):
        with pytest.raises(DatabaseError):
            save_deep(
                DatasetTag(dataset=Dataset(name="Q3"), tag=Tag(name=too_long)),
                operation=None,
                sources=[],
            )

        assert Dataset.objects.for_user(user).count() == 0
