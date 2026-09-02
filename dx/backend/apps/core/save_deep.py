"""`save_deep(obj, operation=…, sources=…)` — write a row and the unsaved rows it points at.

    link = save_deep(
        DatasetTag(dataset=Dataset(name="Q3"), tag=Tag(name="finance")),
        operation=None,
        sources=[],
    )                       # dataset, tag, then the link — one transaction

Django refuses to save a row whose foreign key points at an unsaved object ("save() prohibited
to prevent data loss due to unsaved related object"), so a tree has to be written from the
bottom up. This walks it: for every foreign key of the object it saves the target first, then
the object, recursively. Circular references are not supported.

Inlined from **django-save-deep** (Tom Deniffel, https://pypi.org/project/django-save-deep/),
which is this walk plus a bare `obj.save()`. Three changes, all of them because a write is a
*version* in this project (`apps/core/models.py`, `.claude/rules/versioning.md`):

- **The two keywords.** `VersionedModel.save()` requires `operation` and `sources` — not
  recording where a row came from has to be a decision, not an accident — so a library that
  calls `save()` with no arguments cannot write here at all. Both are passed to *every*
  versioned row in the tree: one call is one step, and if a source of the step changed, every
  row the call produced would have to be recomputed. Rows that are not `VersionedModel`s
  (`accounts.User`, a `ContentType`) are saved plainly; there is no version to describe.
- **Rows that already exist are left alone.** Upstream re-saves every target it walks, which is
  harmless in plain Django and not here: re-saving an unchanged row would bump its version,
  write an event row and hang the call's `sources` on it. Only `_state.adding` children are
  written; the object handed in is always saved, insert or update.
- **One transaction.** A half-written tree — children in the database, the row that needed them
  not — is a state nothing can clean up, because the children have version history by then.

The tenant column is not this module's business: `OwnedModel.save()` fills `owner` in from the
tenant context, row by row, exactly as it does for every other write. Foreign keys are read
through the field cache rather than through the attribute, so an unset non-null one (an owner
that is about to be filled in) is passed over instead of raising `RelatedObjectDoesNotExist`.
"""

from collections.abc import Sequence

from django.db import models, transaction

from apps.core.models import VersionedModel


def save_deep[ModelT: models.Model](
    obj: ModelT,
    *,
    operation: str | None,
    sources: Sequence[VersionedModel] | None,
    operation_description: str | None = None,
) -> ModelT:
    """Save `obj` and every unsaved row it points at, children first. Returns `obj`, saved.

    `operation`, `sources` and `operation_description` mean what they mean on
    `VersionedModel.save()` — read that docstring for what to write — and reach every versioned
    row the call writes.
    """
    with transaction.atomic():
        _save_tree(obj, operation, sources, operation_description)
    return obj


def _save_tree(
    obj: models.Model,
    operation: str | None,
    sources: Sequence[VersionedModel] | None,
    operation_description: str | None,
) -> None:
    for field in obj._meta.fields:
        if not isinstance(field, models.ForeignKey):
            continue
        related = field.get_cached_value(obj, default=None)
        # `_state.adding`, not `pk is None`: every primary key here has a `db_default`
        # (`uuidv7()`), so an unsaved row's pk is a `DatabaseDefault` sentinel, never None.
        if related is not None and related._state.adding:
            _save_tree(related, operation, sources, operation_description)
            setattr(obj, field.name, related)  # the insert has given it a pk; copy it over
    if isinstance(obj, VersionedModel):
        obj.save(operation=operation, sources=sources, operation_description=operation_description)
    else:
        obj.save()
