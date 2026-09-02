---
name: model-examples
description: Write or fix a dx model's `example()` static method and save example trees with `save_example()`. Use when adding or changing a model or one of its fields, when a test, seed script, demo or shell session needs an instance of a model, and when `manage.py check_examples` or the system check `example.E001` fails.
---

# `Model.example()` — one saveable instance of every model

Every model in `backend/apps/` carries a static `example()` that returns **one filled-in,
unsaved instance of itself**. A model that needs another row builds it by calling *that* model's
`example()`, so one call hands back a whole tree, and `save_example()` writes the tree in
dependency order. Machinery and the short version: `backend/apps/core/examples.py`.

```python
from apps.core.examples import save_example

with acting_as(user):                              # tests; a request has the context already
    note = save_example(Note.example())            # a Note in the database, id and all
    link = save_example(DatasetTag.example())      # a Dataset, a Tag, then the link
```

The point is that *any* model can be materialised without knowing anything about it: a test that
needs "some dataset" does not invent one, a demo has something to show, and
`manage.py check_examples` can prove that every model in the project is still writable at all.
A model whose example stopped saving is a model nobody can create.

## Writing one

Rules — the first two are enforced by `example.E001`, the rest by `manage.py check_examples`:

1. **`@staticmethod`, no arguments**, returning its own model. The caller changes what it cares
   about on the returned object (`dataset = Dataset.example(); dataset.name = "…"`); do not add
   parameters, defaults or `**overrides`.
2. **Never set `owner`, `id`, `created`, `modified` or `version`.** The tenant context fills in
   the owner at save time and the database owns the other four (`apps/core/models.py`).
   An example belongs to whoever saves it.
3. **Fill in every required field**, so the object saves as it is. Values are read by humans in
   demos and failing tests: realistic and English (`"Quarterly revenue"`, not `"foo"`).
4. **A required foreign key is another `example()`**, never a query and never `None`.
5. **A field under a unique constraint gets `unique("…")`** (`apps/core/examples.py`), which
   appends a short random suffix. Two examples of one model are saved side by side often enough
   that a constant would be a trap.
6. **No database access.** `example()` builds objects. The one exception is a required key to a
   row that must already exist — a `ContentType`; say why in a comment (`core.Lineage` is the
   only case so far).

### The shapes, as they occur in this project

```python
# Plain fields, and a typed JSON column: the pydantic model, not a dict (apps/datasets/models.py)
@staticmethod
def example() -> Dataset:
    return Dataset(
        name="Quarterly revenue",
        description="Rows imported from the finance export.",
        row_count=3,
        options=DatasetOptions(delimiter=";"),
    )

# Unique per owner, so not a constant
@staticmethod
def example() -> Tag:
    return Tag(name=unique("finance"))

# A join: both sides are examples, and `save_example` writes them first
@staticmethod
def example() -> DatasetTag:
    return DatasetTag(dataset=Dataset.example(), tag=Tag.example())

# A file: the bytes are part of the example, and saving really writes them to storage
@staticmethod
def example() -> Document:
    content = b"name,amount\nrent,1200\n"
    return Document(
        file=ContentFile(content, name="expenses.csv"),
        name="expenses.csv",
        content_type="text/csv",
        size=len(content),
    )
```

## Saving one

`save_example(obj)` (`apps/core/examples.py`) saves the target of every unsaved foreign key
first and the object last, and fills in the tenant column, which no example sets. Use it wherever
an example is written: tests, seed and demo scripts, a shell session, a management command.

- **A tenant context is required** for anything owned: `with acting_as(user):` in tests,
  `tenant_context(user_id)` elsewhere, the middleware in a request. Without one you get
  `NoTenantContext`, not a silent write.
- The walk is `save_deep(obj, operation=…, sources=…)` (`apps/core/save_deep.py`):
  django-save-deep, inlined and taught the two keywords, because every write here states its
  lineage (`VersionedModel.save`). Use it directly for any hand-built tree; `save_example` is it
  with `operation=None, sources=[]` — an example is a fixture, built from nothing by nobody —
  plus the tenant column.
- It writes only the children that are unsaved (`_state.adding`, never `pk is None`: every pk has
  a `db_default`, so an unsaved row's pk is a `DatabaseDefault` sentinel), and writes the whole
  tree in one transaction.
- Rolling back: examples are ordinary writes — versioned, soft-deletable, tenant-scoped. Nothing
  about them is special-cased in the database.

## Using them in tests

The existing rule stands: a test **about** a service function creates its data through that
function (`create_dataset_for(user, name=…)` — `.claude/rules/testing.md`). `example()` is for
the rows a test merely needs to *exist*:

```python
def test_other_users_get_404(user: User, other_user: User, client_for) -> None:
    with acting_as(user):
        dataset = save_example(Dataset.example())      # not: Dataset.objects.create(owner=user, …)
    assert client_for(other_user).get(f"/api/datasets/{dataset.pk}").status_code == 404
```

That is what `manage.py newapp` scaffolds into a new app's `test_api.py`, and why a raw
`Model.objects.create(owner=user, …)` in a test is now a smell: it repeats field values that the
model already knows and it names the owner the context already carries.

## When it breaks

- `example.E001` (system check, runs on every `manage.py …`) — a model has no `example()` of its
  own. An inherited one does not count: `VersionedModel.example()` raises.
- `manage.py check_examples` (run by `./scripts/check.sh`) — builds every example and saves each
  tree in its own savepoint, rolled back again, against the real database. It fails when a
  required field was added without a default, when a constraint rejects the example, or when a
  foreign key lost its target. Fix the example, not the check.
- `apps/core/tests/test_examples.py` is the same two assertions inside the suite.

**After changing a model's fields, re-read its `example()`** — a new required field belongs in
it, a removed field must come out of it, and a new unique constraint means `unique("…")`.
