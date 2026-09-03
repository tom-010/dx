"""Assertions other apps' tests use instead of querying `TimelineEvent` themselves.

    from apps.timeline.testing import assert_event, assert_no_event

    assert_event(document, "documents.uploaded", title="scan.pdf")
    assert_no_event(document)

Keeping the query in one place means a change to how the projection is stored is a change to
this file, not to every test in every app that emits events.
"""

from apps.core.models import OwnedModel
from apps.timeline.models import TimelineEvent
from apps.timeline.services import events_for


def assert_event(source: OwnedModel, key: str, **expected: object) -> TimelineEvent:
    """The source's live event of type `key`, with the given fields. Returns it, so a test can
    go on to assert something the keyword form cannot express."""
    event = events_for(source).filter(event_type=key).first()
    assert event is not None, (
        f"no live {key!r} event for {type(source).__name__} {source.pk}; "
        f"it has {sorted(events_for(source).values_list('event_type', flat=True))}"
    )
    for name, value in expected.items():
        actual = getattr(event, name)
        assert actual == value, f"{key}.{name} is {actual!r}, expected {value!r}"
    return event


def assert_no_event(source: OwnedModel, key: str | None = None) -> None:
    """The source has no live events — none at all, or none of type `key`."""
    events = events_for(source)
    if key is not None:
        events = events.filter(event_type=key)
    found = sorted(events.values_list("event_type", flat=True))
    assert not found, f"{type(source).__name__} {source.pk} still has {found}"
