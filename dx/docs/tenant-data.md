# One tenant's data: export, import, erase

**A tenant is a user.** Every feature row carries an `owner`, so "all the data of one user" is a
set the database can name exactly — which is what makes a per-user backup, a per-user restore
and a per-user erasure possible at all. The mechanics of that ownership are in
`.claude/rules/multitenancy.md`; this page is the operator's view.

Three commands, all in `apps/core`:

| | Command | Needs |
|---|---|---|
| Export | `manage.py pull_tenant USER [-o FILE] [--no-scrub] [--with-files]` | the runtime role |
| Import | `manage.py load_tenant FILE` | the runtime role |
| Erase | `DB_ROLE=migrator manage.py delete_tenant USER [-y]` | cross-tenant credentials |

They are not the whole-database backup. `manage.py backup` / `restore` (see
`.claude/rules/backups.md`) dump *everything*; these three work on one user.

## What "all the data" means

An export contains, for one user:

- the `accounts.User` row itself,
- every owned row — soft-deleted ones included, because a row the application has stopped
  showing is still data we hold,
- **the version history**: the event table rows mirroring all of the above
  (`.claude/rules/versioning.md`),
- **the lineage edges** between those versions,
- with `--with-files`, the uploaded objects themselves: every storage key any *version* of any
  row points at, so restoring an old version still resolves to a file.

In other words `apps/core/rls.py::isolated_models()` — everything the tenant policy protects.
Nothing else can be in there: `pull_tenant` runs as the runtime role with the tenant pinned, so
row-level security makes another user's rows unreadable even if a query forgot a filter.

## Back up and restore one user

```bash
# export — rows and files, exactly as they are
cd backend
uv run python manage.py pull_tenant alice --no-scrub --with-files -o alice.zip

# ...restore it, here or in another environment
uv run python manage.py load_tenant alice.zip
```

`--with-files` writes a zip (`tenant.json` plus `files/<storage key>`) instead of a bare JSON
fixture; `load_tenant` takes either form and restores the objects to the exact keys the rows
name. **Without it you get the rows only**, pointing at keys that may not exist in the target
bucket — fine for dev parity, not a backup.

`--with-files` requires `--no-scrub`, because file *contents* cannot be anonymised: bundling
them while the rows around them are anonymised would be a lie about what the archive holds.

A load **overwrites rows with the same primary key and deletes nothing**. Restoring on top of a
user who has since created more rows merges the two; it does not roll them back. To get the
state at export time exactly, erase the user first and then load.

Version history survives the round trip unchanged: the fixture carries each row's `version` and
its event rows, and the load replays them without bumping anything or writing history a second
time (`apps.core.history.unversioned()`).

## Copy production data into development

```bash
# on production — anonymised (the default): names, emails and other PII are replaced
uv run python manage.py pull_tenant alice -o alice.json
# locally
uv run python manage.py load_tenant alice.json
```

Scrubbing is `apps/core/scrub.py`: every field whose name looks like PII needs an entry in
`SCRUBBERS` or the export refuses to run, so a new personal field cannot quietly start leaving
the building. `--no-scrub` skips it and needs a reason you would be willing to write down.

## Erase a user

```bash
cd backend
DB_ROLE=migrator uv run python manage.py delete_tenant alice     # asks first; -y to skip
```

This is the one operation in the project that really deletes. It removes the user, every owned
row, **their version history and lineage edges**, and the files those rows referenced — rows in
one transaction, then the files once it commits (a rollback that had already deleted objects
would leave rows pointing at nothing; the reverse leaves a harmless orphan in the bucket).

It needs credentials the row-level security policies do not apply to (`DB_ROLE=migrator`),
because the cascade has to *see* the rows. **This is also why deleting a user in the Django
admin does not work** — it reports success and then fails at the foreign keys on commit.

Take an export first if the erasure has to be reversible:

```bash
uv run python manage.py pull_tenant alice --no-scrub --with-files -o alice.zip
DB_ROLE=migrator uv run python manage.py delete_tenant alice -y
uv run python manage.py load_tenant alice.zip     # ...if it turns out you needed it back
```

## Answering "what do you hold about me"

`pull_tenant --no-scrub` is that answer, and deliberately includes soft-deleted rows and the
full version history — everything the database still holds, not everything the UI still shows.
`delete_tenant` is the matching erasure: history that outlived an erased user would defeat it.

## Where the code is

- `apps/core/tenants.py` — `owned_rows`, `tenant_summary`, `delete_tenant`, and the archive
  (`tenant_files`, `write_archive`, `unpack_archive`).
- `apps/core/management/commands/` — `pull_tenant.py`, `load_tenant.py`, `delete_tenant.py`.
- `apps/core/scrub.py` — the PII allowlist and the scrubbers.
- Tests: `apps/core/tests/test_tenancy.py` (round trip, erasure, isolation).
