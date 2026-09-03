"""The one thing this app needs `ready()` for: finding the notification types.

Every app that sends notifications has a `notification_types.py`; importing it is what
registers its types (`contracts.registry`). Doing that here rather than in each app's own
`ready()` keeps the convention to one file per module and nothing in settings.
"""

from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules


class NotificationsConfig(AppConfig):
    name = "apps.notifications"

    def ready(self) -> None:
        from apps.notifications import checks  # noqa: F401, PLC0415 - registers notification.E00x

        # Imports `<app>/notification_types.py` wherever one exists. Models are all loaded by
        # now, so a module is free to import its own (and only its own).
        autodiscover_modules("notification_types")
