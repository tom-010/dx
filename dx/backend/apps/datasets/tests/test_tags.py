"""Tags: the project's one explicit many-to-many, and the cascade rules that go with it.

`DatasetTag` is a real owned, versioned model rather than an auto-created join table, because a
tag change has to leave a version row behind it like every other write (CLAUDE.md "Versioning,
history and lineage"). That makes the join rows ordinary tenant data — and it makes deleting
application logic, since Django's collector no longer runs.
"""

import pytest
from django.test import Client

from apps.accounts.models import User
from apps.core.testing import acting_as
from apps.datasets.api import create_dataset_for, set_dataset_tags
from apps.datasets.models import Dataset, DatasetId, DatasetTag, Tag

pytestmark = pytest.mark.django_db


def test_create_with_tags(auth_client: Client) -> None:
    created = auth_client.post(
        "/api/datasets",
        {"name": "Orders", "tags": ["sales", "2026"]},
        content_type="application/json",
    )

    assert created.status_code == 201
    assert created.json()["tags"] == ["2026", "sales"]  # sorted, case-insensitively


def test_patch_replaces_the_whole_tag_set(auth_client: Client, user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="Orders", tags=["sales", "old"])

    patched = auth_client.patch(
        f"/api/datasets/{dataset.pk}",
        {"tags": ["sales", "new"]},
        content_type="application/json",
    )

    assert patched.status_code == 200
    assert patched.json()["tags"] == ["new", "sales"]
    with acting_as(user):
        # "old" lost its last link, so it is retired — but the row and its history remain.
        assert sorted(Tag.objects.values_list("name", flat=True)) == ["new", "sales"]
        assert Tag.all_objects.get(name="old").deleted_at is not None


def test_patching_only_tags_does_not_bump_the_dataset(auth_client: Client, user: User) -> None:
    """A tag edit is a change to the join rows, not to the dataset row: bumping the dataset
    would add a revision to its history in which nothing about it changed."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="Orders")

    auth_client.patch(
        f"/api/datasets/{dataset.pk}", {"tags": ["sales"]}, content_type="application/json"
    )

    with acting_as(user):
        dataset.refresh_from_db()
    assert dataset.version == 1


def test_put_without_tags_clears_them(auth_client: Client, user: User) -> None:
    """PUT replaces everything, tags included — omitted means "no tags", like every other field."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="Orders", tags=["sales"])

    replaced = auth_client.put(
        f"/api/datasets/{dataset.pk}", {"name": "Orders"}, content_type="application/json"
    )

    assert replaced.status_code == 200
    assert replaced.json()["tags"] == []


def test_tags_are_matched_case_insensitively_and_deduplicated(user: User) -> None:
    with acting_as(user):
        first = create_dataset_for(user, name="a", tags=["Sales", " sales ", "SALES"])
        second = create_dataset_for(user, name="b", tags=["sales"])

        assert first.tag_names() == ["Sales"]  # stored as first typed
        assert second.tag_names() == ["Sales"]
        assert Tag.objects.count() == 1  # one row, not three


def test_a_retired_tag_name_can_be_used_again(user: User) -> None:
    """The unique constraint is conditional, so a retired tag does not squat on its name."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="a", tags=["sales"])
        retired = Tag.objects.get(name="sales")

        set_dataset_tags(user, dataset, [])
        assert Tag.objects.filter(name="sales").count() == 0

        set_dataset_tags(user, dataset, ["sales"])
        revived = Tag.objects.get(name="sales")

    assert revived.pk != retired.pk  # a new row, with its own version chain


def test_soft_deleting_a_dataset_takes_its_links_with_it(user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="Orders", tags=["sales"])
        delete_dataset_in_tests(user, dataset)

        assert DatasetTag.objects.count() == 0
        assert Tag.objects.count() == 0
        # Nothing was actually removed: every row is still there, one version further on.
        assert DatasetTag.all_objects.get().deleted_at is not None
        assert Tag.all_objects.get().deleted_at is not None


def test_tag_links_are_read_through_the_owned_manager(user: User) -> None:
    """`dataset.tags` (Django's m2m descriptor) applies the *target* model's manager but queries
    the join table raw, so it cannot tell that a link was soft-deleted. That is the trap this
    project's "read `tag_links`, never `tags`" convention exists to avoid.

    Two datasets share the tag, so removing it from one leaves the `Tag` row alive and only the
    link retired — exactly the case where the two answers differ.
    """
    with acting_as(user):
        keeper = create_dataset_for(user, name="Keeps it", tags=["sales"])
        dropper = create_dataset_for(user, name="Drops it", tags=["sales"])
        set_dataset_tags(user, dropper, [])

        assert keeper.tag_names() == ["sales"]
        assert dropper.tag_names() == []  # correct
        assert dropper.tags.count() == 1  # the trap: the retired link is still counted


def test_tags_are_tenant_isolated(user: User, other_user: User) -> None:
    with acting_as(user):
        create_dataset_for(user, name="mine", tags=["shared-name"])
    with acting_as(other_user):
        create_dataset_for(other_user, name="theirs", tags=["shared-name"])
        assert Tag.objects.count() == 1  # each tenant has its own row of the same name
    with acting_as(user):
        assert Tag.objects.count() == 1


def test_tag_limit_is_enforced_by_the_schema(auth_client: Client) -> None:
    too_many = [f"tag{index}" for index in range(30)]
    response = auth_client.post(
        "/api/datasets", {"name": "Orders", "tags": too_many}, content_type="application/json"
    )
    assert response.status_code == 422


def test_pruning_only_touches_the_callers_tags(user: User, other_user: User) -> None:
    with acting_as(other_user):
        create_dataset_for(other_user, name="theirs", tags=["keep"])
    with acting_as(user):
        dataset = create_dataset_for(user, name="mine", tags=["drop"])
        set_dataset_tags(user, dataset, [])
    with acting_as(other_user):
        assert [tag.name for tag in Tag.objects.all()] == ["keep"]


def test_dataset_out_reports_tags_and_version(auth_client: Client, user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="Orders", tags=["sales"])
        Dataset.objects.filter(pk=dataset.pk).update(row_count=5)

    body = auth_client.get(f"/api/datasets/{dataset.pk}").json()

    assert body["tags"] == ["sales"]
    assert body["version"] == 2  # the bulk update was versioned like any other write
