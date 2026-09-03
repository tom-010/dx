"""The inbox's contract: notify is an upsert, reading sticks, removal is soft."""

import pytest

from apps.accounts.models import User
from apps.core import lineage
from apps.core.testing import acting_as
from apps.datasets.api import create_dataset_for, delete_dataset_for
from apps.datasets.models import Dataset, DatasetId
from apps.datasets.notification_types import DATASET_CREATED
from apps.notifications import services
from apps.notifications.contracts import (
    InvalidNotificationType,
    NotificationData,
    NotificationType,
    NotificationTypeRegistry,
    UnknownNotificationType,
)
from apps.notifications.models import Notification
from apps.notifications.testing import assert_not_notified, assert_notified

pytestmark = pytest.mark.django_db


def test_creating_a_dataset_notifies_its_owner(user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="Orders", description="From the shop export")

        notification = assert_notified(
            dataset,
            DATASET_CREATED,
            title="New dataset: Orders",
            description="From the shop export",
        )
        assert notification.owner_id == user.pk
        assert notification.source_model == "datasets.dataset"
        assert notification.source_id == dataset.pk
        assert notification.read_at is None  # it arrives unread


def test_the_description_is_optional(user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="Orders")
        assert_notified(dataset, DATASET_CREATED, description="")


def test_reading_is_idempotent_and_survives_a_refresh(user: User) -> None:
    """The one piece of state a notification owns: `notify` may rewrite the wording, never the
    fact that the reader has already seen it."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="Orders")
        notification = assert_notified(dataset, DATASET_CREATED)

        notification.mark_read()
        first_read = notification.read_at
        assert first_read is not None
        notification.mark_read()
        assert notification.read_at == first_read

        dataset.name = "Orders 2026"
        dataset.save(operation=None, sources=[])
        again = services.notify(DATASET_CREATED, dataset)

        assert again.pk == notification.pk  # one message, not two
        assert again.title == "New dataset: Orders 2026"
        assert again.read_at == first_read
        assert Notification.objects.count() == 1


def test_deleting_the_dataset_retires_its_notification(user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="Orders")
        delete_dataset_for(user, DatasetId(dataset.pk))

        assert_not_notified(dataset)
        assert Notification.all_objects.deleted().count() == 1


def test_only_the_named_notifications_are_read(user: User) -> None:
    """The inbox marks the page it is showing, so what it has not shown stays unread."""
    with acting_as(user):
        first = create_dataset_for(user, name="A")
        create_dataset_for(user, name="B")
        shown = assert_notified(first, DATASET_CREATED)

        assert services.unread_for(user).count() == 2
        assert services.mark_read(user, [shown.pk]) == 1
        assert services.unread_for(user).count() == 1
        assert services.mark_read(user, [shown.pk]) == 0  # already read, not counted twice


def test_another_users_ids_are_ignored(user: User, other_user: User) -> None:
    """Not a 404: the queryset is the caller's, so a foreign id simply matches nothing."""
    with acting_as(other_user):
        theirs = assert_notified(create_dataset_for(other_user, name="Theirs"), DATASET_CREATED)
    with acting_as(user):
        create_dataset_for(user, name="Mine")
        assert services.mark_read(user, [theirs.pk]) == 0
        assert services.unread_for(user).count() == 1
    with acting_as(other_user):
        assert services.unread_for(other_user).count() == 1  # untouched


def test_a_notification_is_not_a_derivation(user: User) -> None:
    """`sources=[]` on purpose: nothing ever recomputes a message, so an edge here would only
    put rows into `stale_derivations()` that no rebuild will ever act on. The timeline, which
    *does* rebuild, records its edge (`apps/notifications/services.py`)."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="Orders")
        notification = assert_notified(dataset, DATASET_CREATED)

        assert notification.sources() == []
        assert lineage.derived_from(dataset).count() == 0


def test_one_inbox_per_tenant(user: User, other_user: User) -> None:
    with acting_as(user):
        create_dataset_for(user, name="Mine")
    with acting_as(other_user):
        assert Notification.objects.count() == 0
        create_dataset_for(other_user, name="Theirs")
        assert [n.title for n in Notification.objects.all()] == ["New dataset: Theirs"]


def test_unknown_keys_raise(user: User) -> None:
    with acting_as(user), pytest.raises(UnknownNotificationType):
        services.notify("datasets.invented", create_dataset_for(user, name="A"))


def test_keys_are_a_namespace() -> None:
    own = NotificationTypeRegistry()

    class First(NotificationType[Dataset]):
        key = "datasets.taken"
        model = "datasets.Dataset"
        label = "First"

        def describe(self, obj: Dataset) -> NotificationData:
            return NotificationData(title=obj.name)

    class Second(First):
        pass

    class Malformed(First):
        key = "NotAKey"

    own.register(First)
    with pytest.raises(InvalidNotificationType, match="both claim"):
        own.register(Second)
    with pytest.raises(InvalidNotificationType, match="app_label"):
        own.register(Malformed)
    assert own.for_model(Dataset) == [own.get("datasets.taken")]
