"""Assertions other apps' tests use instead of querying `Notification` themselves.

from apps.notifications.testing import assert_notified, assert_not_notified

assert_notified(dataset, "datasets.created", title="New dataset: Orders")
assert_not_notified(dataset)
"""

from apps.core.models import OwnedModel
from apps.notifications.models import Notification
from apps.notifications.services import notifications_for


def assert_notified(source: OwnedModel, key: str, **expected: object) -> Notification:
    """The source's live notification of type `key`, with the given fields. Returns it."""
    notification = notifications_for(source).filter(notification_type=key).first()
    assert notification is not None, (
        f"no live {key!r} notification for {type(source).__name__} {source.pk}; "
        f"it has {sorted(notifications_for(source).values_list('notification_type', flat=True))}"
    )
    for name, value in expected.items():
        actual = getattr(notification, name)
        assert actual == value, f"{key}.{name} is {actual!r}, expected {value!r}"
    return notification


def assert_not_notified(source: OwnedModel, key: str | None = None) -> None:
    """The source has no live notifications — none at all, or none of type `key`."""
    notifications = notifications_for(source)
    if key is not None:
        notifications = notifications.filter(notification_type=key)
    found = sorted(notifications.values_list("notification_type", flat=True))
    assert not found, f"{type(source).__name__} {source.pk} still has {found}"
