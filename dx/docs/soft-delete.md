# Soft delete: the API

**Deleting is soft.** `deleted_at` is a column on every `VersionedModel` (so: everything except
`accounts.User`, which is Django's `AbstractUser`), `delete()` sets it, and a real `DELETE` has
to be asked for by name. Why it works this way is in `docs/history_lineage_delete_tenants.md`; the invariants are in
`.claude/rules/versioning.md`; this page is the API.

Two things follow from it that are easy to get wrong, so they are stated here up front:

- **A delete is a write.** It bumps `version` and writes a version row like any other change,
  because "was deleted" is a state the row had and lineage edges pointing at earlier versions
  must keep resolving.
- **Soft-deleted rows are already filtered out.** `Model.objects` excludes them; seeing them is
  the thing you have to ask for.

## Deleting

```python
dataset.delete()                          # soft; `soft_delete()` is the same thing, spelled out
Dataset.objects.filter(...).delete()      # soft, in one UPDATE
```

Both go through the versioned write path, so each deleted row is at the next version with
`deleted_at` set and an event row saying so. The queryset form skips rows that are already
deleted — deleting twice is not an event — and does **not** cascade: cascade is application
logic in this project.

A service method that deletes calls it and nothing else:

```python
get_note_for(user, note_id).soft_delete()
return Status(204, None)
```

`Model.objects` stops returning the row immediately, so a second `DELETE` of the same id is a
404 exactly as it would be for a row that never existed.

A **real** delete is a different verb:

```python
dataset.hard_delete()                          # this row and its version rows
Dataset.all_objects.filter(...).hard_delete()  # ...in bulk
```

Three callers mean it: tenant erasure (`apps/core/tenants.py`), purging spent refresh tokens
(`apps/accounts/api.py` — a credential's history is the credential), and test teardown.

Everything the Python override cannot reach is still refused by the database:

```python
Dataset._base_manager.filter(pk=dataset.pk).delete()   # or raw SQL, or a data migration
# django.db.utils.ProgrammingError: pgtrigger: Cannot delete rows from datasets_dataset table
```

That guard is `pgtrigger.Protect(name="no_hard_delete")` on `VersionedModel.Meta` (and on
`Lineage`). `hard_delete()` lifts it for the duration; `history.hard_delete()` is the same lift
as a context manager, for `loaddata` and migrations.

## Reading

| Call | Tenant scope | Soft-deleted rows |
|---|---|---|
| `Model.objects` | yes (`OwnedManager`) | **hidden** |
| `Model.all_objects` | yes | included |
| `Model.all_objects.deleted()` | yes | only those |
| `Model._base_manager` | no | included |

```python
ModelB.objects.count()               # 14 — what the application sees
ModelB.all_objects.count()           # 92 — everything this tenant owns
ModelB.all_objects.deleted().count() # 78 — only the retired ones
```

(Real numbers from a database with `manage.py lineage_demo --clean` run a few times: most of a
table is retired rows sooner than you would think, which is why the default matters.)

`alive()`, `deleted()` and `hard_delete()` live on `ActiveQuerySet`; the managers narrow
`all()` and `filter()` to return it, so they survive the hop through `Model.objects`.

`_base_manager` is Django's own, and is **deliberately left unfiltered**: it is what forward
foreign key traversal, `refresh_from_db` and the deletion collector use, and `document.owner`
has to keep resolving after the owner has been soft-deleted — in code paths nobody wrote. Tenant
isolation on that path is row-level security, not the manager. Application code should not use
it; tooling that must see every row (the explorer, the export) does.

## Restoring

There is no `restore()`. Putting a row back is an ordinary write:

```python
note.deleted_at = None
note.save()
```

It gets the **next** version number rather than resurrecting the old one, so the chain still
reads forwards and the deleted state stays in the history where it happened. To restore the
*values* of an earlier version as well, take them from the history:

```python
original = note.history()[0].to_object()   # a Note, as it was created — unsaved
note.name = original.name
note.deleted_at = None
note.save()
```

`manage.py lineage_demo`'s `restore` shape does exactly this, and leaves a row whose four
versions read `[live, live, deleted, live]`.

## What a delete does *not* do

- **It does not cascade.** Django's collector never runs for a soft delete, so a service
  decides per relation:
  block, reassign, or soft-delete the children by hand. `apps/datasets/api.py` is the worked
  example — deleting a dataset retires its tag links, and a tag that lost its last link is
  retired with them. Never do this in a signal: it would also fire on the restore and erasure
  paths, where it is exactly wrong.
- **It does not free a unique value** unless the constraint says so. Every unique constraint is
  therefore conditioned on `deleted_at__isnull=True`, or a retired row would reserve its name
  forever. `unique=True` and `unique_together` on a versioned model fail a test.
- **It does not delete files.** Earlier versions still reference the storage key; only tenant
  erasure reclaims objects.
- **It does not break lineage.** An edge names a *version*, and that version is still there. The
  explorer marks such a source `superseded · deleted` and keeps the link to the version that was
  consumed.

## Really deleting a whole tenant

`obj.hard_delete()` removes one row and its own history — not the history of whatever the
cascade collected. Erasing a person is `manage.py delete_tenant`, which walks every table of one
owner (`apps/core/tenants.py`, `docs/tenant-data.md`). Being able to do that cleanly is the
reason tenancy is defined the way it is: one user's rows are a disjoint subgraph.

## Seeing what is deleted

`/explorer/` (development only) lists **live rows by default**, matching `Model.objects`, and
says in the table's caption how many retired rows that hid, with a link to them — plus
Live / Deleted / All buttons and a `Retired` column on the model list. A deleted row is marked
in its own heading and at the far end of every lineage edge that points at it.

## Where the code is

- `apps/core/models.py` — `VersionedModel.deleted_at` / `soft_delete()`, the `no_hard_delete`
  trigger, `ActiveQuerySet.alive()/deleted()`, `ActiveManager`, `OwnedManager`,
  `AllOwnedManager`.
- `apps/core/history.py` — `hard_delete()`.
- `apps/datasets/api.py` — cascade written out by hand (`set_dataset_tags`, `prune_unused_tags`).
- `apps/core/explorer.py` — the development UI described above.
- Tests: `apps/core/tests/test_history.py` (a delete is a version, hard delete refused, forward
  FK still resolves, unique values freed), `apps/core/tests/test_explorer.py` (what the pages
  show).
