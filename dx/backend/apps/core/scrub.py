"""Anonymisation for `manage.py pull_tenant`: which fields hold personal data and what replaces
them in an exported fixture.

`PII_FIELDS` is an allowlist of field *names*; every model field with one of these names must
have an entry in `SCRUBBERS`, otherwise `scrub()` refuses — a new PII field fails loudly instead
of leaking into a dump. `check_scrubbers()` runs the same check over the whole model registry
(apps/core/tests/test_tenancy.py). Placeholders are deterministic (no faker dependency).
"""

from collections.abc import Callable

from django.apps import apps
from django.contrib.auth.hashers import make_password
from django.db import models

Scrubber = Callable[[models.Model, int], object]  # (instance, running number) -> new value

# Field names that carry personal data wherever they appear.
PII_FIELDS: frozenset[str] = frozenset(
    {"password", "email", "first_name", "last_name", "last_login", "phone", "address"}
    # A recorded HTTP request (apps/core/request_record.py): what the client sent is whatever
    # the client sent, and the headers name their machine. Distinctive names on purpose — this
    # list matches field names across every model, and `body` is also a note's text.
    | {"sent_headers", "sent_query", "sent_body"}
)

# "app_label.modelname" -> field -> replacement.
SCRUBBERS: dict[str, dict[str, Scrubber]] = {
    "core.requestrecord": {
        # Shape kept, content gone: the method, path and status still say what happened.
        "sent_headers": lambda obj, n: (
            {"Content-Type": kind} if (kind := getattr(obj, "content_type", "")) else {}
        ),
        "sent_query": lambda obj, n: {},
        "sent_body": lambda obj, n: None,
    },
    "accounts.user": {
        "password": lambda obj, n: make_password(None),  # unusable: nobody can log in
        "email": lambda obj, n: f"user{n}@example.invalid",
        "first_name": lambda obj, n: "",
        "last_name": lambda obj, n: "",
        "last_login": lambda obj, n: None,
    },
}


class UnscrubbedField(Exception):
    """A PII field has no scrubber: add one to SCRUBBERS before exporting."""


def _label(model: type[models.Model]) -> str:
    return model._meta.label_lower


def pii_fields(model: type[models.Model]) -> list[str]:
    return [field.name for field in model._meta.fields if field.name in PII_FIELDS]


def missing_scrubbers(model: type[models.Model]) -> list[str]:
    known = SCRUBBERS.get(_label(model), {})
    return [name for name in pii_fields(model) if name not in known]


def check_scrubbers() -> list[str]:
    """`"app.Model.field"` for every PII field without a scrubber, over all installed models."""
    return [
        f"{_label(model)}.{field}"
        for model in apps.get_models()
        for field in missing_scrubbers(model)
    ]


def scrub(obj: models.Model, number: int = 0) -> models.Model:
    """Replace the PII fields of `obj` in place (it is not saved) and return it."""
    model = type(obj)
    missing = missing_scrubbers(model)
    if missing:
        raise UnscrubbedField(
            f"{_label(model)} has PII fields without a scrubber: {', '.join(missing)}"
        )
    for field, scrubber in SCRUBBERS.get(_label(model), {}).items():
        setattr(obj, field, scrubber(obj, number))
    return obj
