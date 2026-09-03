"""The one thing this app needs `ready()` for: finding the event types.

Every app that puts something on the timeline has a `timeline_events.py`; importing it is what
registers its types (`contracts.registry`). Doing that here rather than in each app's own
`ready()` keeps the convention to one file per module and nothing in settings.
"""

from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules


class TimelineConfig(AppConfig):
    name = "apps.timeline"

    def ready(self) -> None:
        from apps.timeline import checks  # noqa: F401, PLC0415 - registers timeline.E00x

        # Imports `<app>/timeline_events.py` wherever one exists. Models are all loaded by now,
        # so a module is free to import its own (and only its own).
        autodiscover_modules("timeline_events")
