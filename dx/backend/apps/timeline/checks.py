"""System checks over the event-type registry (`manage.py check`, and every test run).

A registry is a global namespace filled in by import side effects, so it needs checking. Two of
the four things worth checking are already covered elsewhere and are deliberately not repeated
here: a malformed or duplicate key is refused by `registry.register` at import time, and a
`payload_schema` that is not a pydantic model is a mypy error at the declaration.

What is left can only be decided once every app is loaded: does `model` still resolve, and does
the key's prefix still name the app the model lives in.
"""

from collections.abc import Sequence

from django.apps import AppConfig
from django.apps import apps as django_apps
from django.core.checks import CheckMessage, Error, Tags, register

from apps.timeline.contracts import registry


@register(Tags.models)
def check_event_types(
    app_configs: Sequence[AppConfig] | None = None, **kwargs: object
) -> list[CheckMessage]:
    errors: list[CheckMessage] = []
    for event_type in registry.all():
        name = type(event_type).__name__
        try:
            model = django_apps.get_model(event_type.model)
        except LookupError, ValueError:
            errors.append(
                Error(
                    f"{name}.model = {event_type.model!r} does not resolve to a model",
                    hint="An event type names its source as a label string, 'app.Model'.",
                    id="timeline.E001",
                )
            )
            continue
        prefix = event_type.key.split(".")[0]
        if model._meta.app_label != prefix:
            errors.append(
                Error(
                    f"{name}.key is prefixed {prefix!r} but its model lives in "
                    f"{model._meta.app_label!r} — a key must say where its code is",
                    id="timeline.E002",
                )
            )
    return errors
