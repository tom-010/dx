"""System checks over the notification-type registry (`manage.py check`, and every test run).

The same two checks as `apps/timeline/checks.py`, for the same reason: a malformed or duplicate
key is already refused by `registry.register` at import time, and what is left can only be
decided once every app is loaded — does `model` still resolve, and does the key's prefix still
name the app the model lives in.
"""

from collections.abc import Sequence

from django.apps import AppConfig
from django.apps import apps as django_apps
from django.core.checks import CheckMessage, Error, Tags, register

from apps.notifications.contracts import registry


@register(Tags.models)
def check_notification_types(
    app_configs: Sequence[AppConfig] | None = None, **kwargs: object
) -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    for notification_type in registry.all():
        name = type(notification_type).__name__
        try:
            model = django_apps.get_model(notification_type.model)
        except LookupError, ValueError:
            errors.append(
                Error(
                    f"{name}.model = {notification_type.model!r} does not resolve to a model",
                    hint="A notification type names its source as a label string, 'app.Model'.",
                    id="notification.E001",
                )
            )
            continue
        prefix = notification_type.key.split(".")[0]
        if model._meta.app_label != prefix:
            errors.append(
                Error(
                    f"{name}.key is prefixed {prefix!r} but its model lives in "
                    f"{model._meta.app_label!r} — a key must say where its code is",
                    id="notification.E002",
                )
            )
    return errors
