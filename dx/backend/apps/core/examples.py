"""One saveable instance of every model: `Model.example()`.

    from apps.core.examples import save_example

    with acting_as(user):
        note = save_example(Note.example())        # a Note, in the database, id and all

Every model in `apps/` carries a static `example()` that returns an **unsaved** instance with
every required field filled in. A model that needs another row builds it by calling *that*
model's `example()`, so one call hands back a whole tree:

    DatasetTag.example()        # a DatasetTag pointing at a fresh Dataset and a fresh Tag

`save_example()` writes that tree in dependency order — children first, then the row
(django-save-deep) — after stamping the tenant column on it.

Why every model, and not a fixture per test: an example is the one place that says what a
filled-in row of this model looks like, so a test that needs "some dataset" does not invent
one, a demo or a seed script has something to show, and `manage.py check_examples` can prove
that every model in the project is still writable at all — a model whose example stopped saving
is a model nobody can create.

Writing one (the long version is `.claude/skills/model-examples`):

- a `@staticmethod` taking no arguments, returning an instance of its own model; change what
  the caller cares about on the returned object rather than parameterising it;
- it builds objects, it does not touch the database — the one exception is a required foreign
  key to a row that must already exist (a `ContentType`);
- never set `owner`, `id`, `created`, `modified` or `version`: the tenant context and the
  database own those (`apps/core/models.py`);
- a field under a unique constraint gets `unique("…")`, so two examples can live side by side.
"""

import uuid
from typing import Any

import pghistory.models
from django.apps import apps
from django.db import models, transaction

from apps.core.db import NoTenantContext, current_user_id, tenant_context
from apps.core.models import OWNER_COLUMN
from apps.core.save_deep import save_deep


class _Rollback(Exception):
    """Raised to undo a savepoint that did what it was supposed to do (`unsaveable_examples`)."""


def unique(text: str) -> str:
    """`text` with a short random suffix — an example value for a field that must be unique.

    Two examples of the same model are saved side by side often enough (a test that needs two
    tags, the check below) that a constant would be a trap: the second save would fail on a
    unique constraint rather than on anything the test is about.
    """
    return f"{text}-{uuid.uuid7().hex[-6:]}"


def example_models() -> list[type[models.Model]]:
    """Every model that must have an `example()`, in a stable order.

    This project's own apps only. The event tables `@tracked` generates are left out: they
    mirror a model that has one, they are written by a trigger and never by hand, and they are
    append-only, so "can you save one" is not a question anyone may ask of them.
    """
    return sorted(
        (
            model
            for model in apps.get_models()
            if apps.get_app_config(model._meta.app_label).name.startswith("apps.")
            and not model._meta.abstract
            and not issubclass(model, pghistory.models.Event)
        ),
        key=lambda model: model._meta.label,
    )


def models_without_example() -> list[str]:
    """Labels of the models that do not define their own `example()`.

    Own, not inherited: `VersionedModel.example()` raises, so an inherited one is exactly the
    case this reports. The system check `example.E001` (`apps/core/checks.py`) is this list.
    """
    return [
        model._meta.label
        for model in example_models()
        if not isinstance(vars(model).get("example"), staticmethod)
    ]


def example_of[ModelT: models.Model](model: type[ModelT]) -> ModelT:
    """`model.example()` for code that has a model class rather than a model — the checks, the
    explorer, a seed script. Raises `TypeError` when the model has no usable example."""
    factory: Any = getattr(model, "example", None)
    if factory is None:
        raise TypeError(f"{model._meta.label} has no example()")
    obj = factory()
    if not isinstance(obj, model):
        raise TypeError(
            f"{model._meta.label}.example() returned {type(obj).__name__}, not {model.__name__}"
        )
    return obj


def save_example[ModelT: models.Model](obj: ModelT) -> ModelT:
    """Save an example and everything it points at, children first. Returns the saved object.

        with acting_as(user):
            link = save_example(DatasetTag.example())   # dataset, tag, then the link itself

    The writing is `save_deep` (`apps/core/save_deep.py`): children first, then the row, in one
    transaction. `operation=None, sources=[]` — an example is a fixture, built from nothing by
    nobody, and says so rather than leaving it to an enclosing `deriving()` block.

    The tenant column is this function's part. `example()` deliberately never sets `owner` — an
    example belongs to whoever saves it, and `OwnedModel.save()` takes that from the tenant
    context like every other write does. `Lineage` carries an owner column without being an
    `OwnedModel`, though, so it has no such fill-in; `_claim` covers the whole tree before a
    single row is written.

    Raises `NoTenantContext` when the tree contains an owned row and no tenant context is
    active (`acting_as(user)` in tests, the middleware in a request).
    """
    _claim(obj, current_user_id.get())
    return save_deep(obj, operation=None, sources=[])


def _claim(obj: models.Model, owner_id: uuid.UUID | None) -> None:
    """Stamp `owner_id` on `obj` and on every unsaved row it points at.

    Reads the foreign keys through the field cache rather than through the attribute, because
    the attribute is what raises on an unset one; a key that was never assigned is simply not
    cached. Rows that are already in the database are left alone — they have an owner.

    Most of the tree would not need this: `OwnedModel.save()` fills the column in from the
    tenant context by itself. `core.Lineage` is why it exists — an owner column on a model that
    is not an `OwnedModel` — and doing it for the whole tree keeps that from being a special
    case nobody remembers.
    """
    for field in obj._meta.fields:
        if isinstance(field, models.ForeignKey):
            related = field.get_cached_value(obj, default=None)
            if related is not None and related._state.adding:
                _claim(related, owner_id)
    if not any(field.attname == OWNER_COLUMN for field in obj._meta.fields):
        return  # a shared model (accounts.User, core.CommandRun): no tenant column to fill
    if obj.__dict__.get(OWNER_COLUMN) is not None:
        return
    if owner_id is None:
        raise NoTenantContext(
            f"No tenant context active; cannot save an example of {type(obj).__name__}. "
            "Wrap the call in acting_as(user) (tests) or tenant_context(user_id)."
        )
    setattr(obj, OWNER_COLUMN, owner_id)


def unsaveable_examples() -> list[str]:
    """Save one example of every model and report what did not work, one line per model.

    Each tree gets its own savepoint and is rolled back again, so the models cannot collide
    with each other and the database is left exactly as it was — which is what makes it safe to
    run against the development database (`manage.py check_examples`, ./scripts/check.sh) and
    not only against the test one (`apps/core/tests/test_examples.py`).
    """
    # Imported here, not at the top: `apps/accounts/models.py` imports this module for
    # `unique()`, so the other direction can only be taken once the registry is populated.
    from apps.accounts.models import User  # noqa: PLC0415

    problems: list[str] = []
    try:
        with transaction.atomic():
            user = User.objects.create_user(unique("example-check"))
            with tenant_context(user.pk):
                problems = [
                    problem
                    for model in example_models()
                    if (problem := _save_and_forget(model)) is not None
                ]
            raise _Rollback
    except _Rollback:
        pass
    return problems


def _save_and_forget(model: type[models.Model]) -> str | None:
    """Save one example inside a savepoint and roll it back; the problem as text, or None."""
    try:
        with transaction.atomic():
            _discard_files(save_example(example_of(model)))
            raise _Rollback
    except _Rollback:
        return None
    except Exception as exc:
        return f"{model._meta.label}: {type(exc).__name__}: {exc}"


def _discard_files(obj: models.Model) -> None:
    """Delete the files the save wrote. Storage is not transactional, so the rollback around it
    would leave an orphaned object in the bucket on every run."""
    for field in obj._meta.fields:
        if isinstance(field, models.FileField):
            stored = getattr(obj, field.name)
            if stored:
                stored.delete(save=False)
