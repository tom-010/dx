---
paths:
  - "**/backend/**"
---

## Versioning, history and lineage (`apps/core/history.py`, `lineage.py`, `revisions.py`)

Every model keeps a full version history, normal queries see only current rows, and **nothing is
ever hard-deleted**. On top of the versions sits a lineage graph whose edges point at a *specific
version* of a source, not at the live row.

| Piece | Where | What it guarantees |
|---|---|---|
| Capture | django-pghistory triggers, `@tracked` (`apps/core/history.py`) | a `.update()`, a `bulk_update`, raw SQL and a data migration all produce version rows — application code cannot bypass it |
| Storage | one event table per model (`DatasetEvent`, …), typed mirrored columns | history is queryable per field, and migrating it is a normal migration |
| Immutability | `PGHISTORY_APPEND_ONLY` → pgtrigger rejects UPDATE and DELETE on event tables | lineage nodes cannot be edited or vanish |
| Version chain | `VersionedModel.version`, bumped by the `bump_version` trigger | authoritative ordering; "which version came first" does not rest on clock behaviour |
| Soft delete | `VersionedModel.deleted_at` + `pgtrigger.Protect` on DELETE | a deleted row stays resolvable, so old lineage edges keep working |
| Isolation | the same RLS policy on every event table and on `Lineage` | history is tenant data (see `.claude/rules/multitenancy.md`) |

- **Tracking a model**: `@tracked` from `apps/core/history.py`, on the concrete model. Never on an
  abstract base — pghistory accepts the decorator there and then generates one concrete event
  model pointing at the abstract class (`fields.E300`) while installing **no triggers at all** on
  the subclasses. `newapp` writes `@tracked` for you. Opting out means adding the label to
  `HISTORY_EXEMPT` with a reason (currently the two `accounts` token models: high-churn
  credential bookkeeping with no lineage value). `test_history.py` fails on anything untracked
  and unlisted, and checks what the event model actually tracks rather than
  `hasattr(pgh_event_model)` — that attribute is inherited.
- **Every version records who wrote it**: `version.stack` (the call stack, as parsed frames),
  `version.release` (the build) and `version.caller` (the innermost frame of this project's
  code) — the same record `Lineage.stack`/`.release`/`.caller` keep for an edge. Python never
  inserts a version row, so the values are handed to the capture trigger through a
  transaction-local setting that `VersionedModel.save()` sets and the `pgh_stack`/`pgh_release`
  column defaults read (`lineage.declare_write_origin`, `history.Event`) — the same mechanism
  pghistory uses for `pgh_context_id`. A write that does not go through `save()` (a bulk
  `.update()`, a data migration, raw SQL) leaves them empty rather than inheriting the
  previous write's, and the explorer's version page says so. **Only this project's frames are
  recorded**, and "this project's" is decided by exclusion: `lineage.is_ours` asks the
  interpreter where *it* lives (`sys.prefix`, `sys.base_prefix`, `sysconfig`, `site` — the venv,
  the stdlib, site-packages) and everything else is ours. No list of our packages to remember
  to extend, and the same answer in the container. The only other exclusion is the recording
  mechanism itself (`lineage.py`, `models.py`); middleware frames stay as tolerated noise. A raw
  request stack is ~45 frames of WSGI, middleware and ninja around one line of ours; what is
  kept is every frame of ours, outermost first — a write five calls deep records the whole
  chain, not its last step. **Each frame also references the source of the function it was
  in** (`StackFrame.sha`, `first_line`): `apps/core/source.py::SourceSnippet` stores the text
  once per distinct function body, content-addressed like a git blob, taken from the running
  interpreter (`co_lines()`, no git needed). Recording is free on the hot path — a process-local
  set, then the shared cache (Redis), then one `INSERT … ON CONFLICT DO NOTHING`; both caches
  are marked on commit only. The explorer folds the numbered function under every frame.
- **Every version and edge written through the API also names its HTTP request**
  (`version.request_id`, `Lineage.request` → `core.RequestRecord`, `apps/core/request_record.py`):
  method, path, query, headers with credentials redacted, and the JSON body (never a file;
  above 64 KB by size only). `TenantMiddleware` scopes the live request in a contextvar; the
  first `save()` of a request records it, in the write's transaction, and stamps its id on every
  version after it through the same `set_config` as the stack. A request that writes nothing
  leaves no row; a task, command or shell has none to record. It is an *owned* table — a body is
  PII — so RLS applies, erasure removes it and `pull_tenant` scrubs `headers`/`query`/`body`.
  The explorer links every history group, version and edge to its request page.
- **Reading history**: `obj.history()` → a list of `history.Version` objects, oldest first
  (`[0]` is the insert, `[-1]` the current state). `version.to_object()` rebuilds the *tracked
  model's own type* from the event row — `document.history()[0].to_object()` is a `Document`,
  typed, unsaved, with typed JSON columns still pydantic instances; saving it back is a restore
  and a normal write (the trigger gives it the next version number). `version.version`, `.at`,
  `.deleted` and `.untracked_fields()` cover the rest; `.event` is the raw row. Underneath:
  `obj.events`, `history.event_rows(Model, pk)` / `history.current_event(obj)`, which return
  rows typed as `history.EventRow` (event models are generated at import time, so a protocol is
  the only way to describe them to mypy). `Model.pgh_event_model` is the class.
- **Never assign `id`, `created`, `modified` or `version` in Python** — all four are database
  defaults or trigger-set (`apps/core/models.py::VersionedModel`). `Now()` renders as
  `STATEMENT_TIMESTAMP()` on Postgres, which is why the trigger uses it too; `NOW()` is the
  transaction start and would put `modified` *before* `created`. `save()` reads `version` and
  `modified` back afterwards, because the instance it returns is what the API serialises — an
  INSERT needs no such read (Django fetches database defaults with RETURNING). A queryset
  `.update()` bypasses all of this by design: call `refresh_from_db()` if you need the values.
- **Deleting** (the API in full: **`docs/soft-delete.md`**; the why: `docs/history_lineage_delete_tenants.md`): `obj.delete()`
  is soft — `soft_delete()` is the same thing spelled out, and `hard_delete()` is the exception. It is an ordinary UPDATE, so
  the `bump_version` trigger fires and the delete gets a version row of its own: "was deleted"
  is a state the row had, at a version, and every earlier version stays readable. There is no
  `restore()` — putting a row back is assigning `deleted_at = None` and saving, which gets the
  *next* version number rather than resurrecting the old one.
  **Filtered out by default, opt in to see them**: `Model.objects` (`ActiveManager` →
  `ActiveQuerySet.alive()`) already excludes soft-deleted rows, `Model.all_objects` keeps the
  tenant scope and includes them, and `.alive()` / `.deleted()` narrow either way
  (`Model.all_objects.deleted()`). Django's
  `_base_manager` (forward FK traversal, `refresh_from_db`, deletion collection) is deliberately
  left unfiltered so `document.owner` still resolves after the owner was soft-deleted — RLS is
  what guards that path. Direct `.delete()` raises **in the database**
  (`pgtrigger.Protect(name="no_hard_delete")` on `VersionedModel.Meta`, and on `Lineage`), so
  raw SQL and the admin's delete button fail too; `history.hard_delete()` is the only lift, for
  tenant erasure and test teardown. Uploaded files are **not** removed on delete — earlier
  versions still reference them; only tenant erasure reclaims them.
- **Every unique constraint is conditioned on `deleted_at__isnull=True`** (see
  `accounts.ApiToken`), otherwise a soft-deleted row reserves its value forever. `unique=True`
  and `unique_together` on a `VersionedModel` fail a test.
- **Cascade is application logic now**: Django's collector never runs, so decide per relation in
  the service layer (block, reassign, or soft-delete the children) — never in a signal, which
  would also fire for the restore and erasure paths, where it is exactly wrong.
  `apps/datasets/api.py` is the worked example: deleting a dataset retires its tag links,
  and a tag that lost its last link is retired with them. Foreign keys between owned models use
  `on_delete=CASCADE`: nothing hard-deletes them except tenant erasure, and `PROTECT` would turn
  that one legitimate delete into a `ProtectedError` (erasure walks the tables in name order).
- **The two escape hatches**, and the only callers allowed to use them:
  `history.hard_delete()` — tenant erasure and test teardown; `history.unversioned()` — `loaddata`
  (`backup`/`restore`, `load_tenant`), where the dump already carries each row's version and its
  event rows and replaying it must not bump them or write history twice.
- **Schema evolution**: `SCHEMA_TAG` names the current tracked field set and every event row
  records the tag it was written under, so the revision page can say "not tracked at this
  version" instead of rendering a backfilled default as data. `backend/history_schema.json` is a
  *log*: every tag keeps the field set it named, older entries are never rewritten. Change a
  tracked field → bump `SCHEMA_TAG` → `makemigrations` (the tag is the `pgh_schema` *column
  default* on **every** event table, so every app with a tracked model gets a migration, not
  just the one you touched) → `manage.py history_schema --write`. A test fails otherwise.
  Dropping a field: archive it into `pgh_archive` in a data migration that runs *before* the
  `RemoveField` (SQL in `apps/core/history.py`), then drop, then bump.
- **Context**: `HistoryMiddleware` opens one per write request and `tenant_task` one per task, so
  everything a single save wrote shares a `pgh_context_id` and renders as one revision.
  Background jobs and shells must open one explicitly (`history.history_context("command", …)`)
  or their rows are attributed to nothing. **Nesting opens a new context** (ours, not
  pghistory's merge): a step inside a request is its own run, which is what `save(operation=…)`
  relies on. **`pghistory_context` is a single shared table every
  tenant can read** — its upsert function needs SELECT and UPDATE on it, so RLS cannot hide it.
  Nothing tenant-identifying goes in the metadata: no user id (the event row's `owner_id` already
  says whose change it was) and no resolved URL (`/api/datasets/<uuid>` names another tenant's
  object). `history_context` raises on a value that looks like an identifier. This is why we do
  **not** use pghistory's own `HistoryMiddleware`, which records both.
- **Lineage** (`apps/core/lineage.py`) **is the default, not a second statement**: every write
  path takes `operation=` and `sources=` — `Model.create(name=…, operation="summarise notes", sources=[a, b])`
  (a classmethod, the shortest spelling), `obj.save(operation=…, sources=…)` for edits, partial
  saves and the PUT/PATCH idiom — and both land in the row's own transaction, against the
  version the write produced. **Both keywords are required** (`None` defers to the enclosing
  blocks, `sources=[]` means "from nothing"; leaving one out is a `TypeError`). `operation`
  is the short, stable *name of the step* a reviewer sees on the lineage node ("summarise
  notes", not "api", not a code location, not the data); `None` when a person made the write.
  `operation_description` (optional) is what the step did in this run. Both live in the shared
  history-context table, so they describe the operation, never the data — `VersionedModel.save`
  has the full guidance. `with deriving(a, b):` names the sources once for every write in
  a block (plain `objects.create()` picks it up through `save()`; it cannot take the keywords
  itself, mypy's Django plugin rejects them); `sources=[]` opts one write out. A bulk
  `.update()` is versioned and labelled but records no edges — no `save()` runs. Underneath
  all of it: `record_derivation(target, sources=[...])` inside the writing transaction. Read it back through the models — `obj.sources()` (what it was built
  from, across all its versions) and `obj.derived()` (what came out of it), both returning
  `history.Version` objects, so `dataset.sources(Document)[0].to_object()` is a `Document` as it
  read then and `.is_current()` says whether it has moved since; passing the model both filters
  and types the result, and `history()[n].sources()` is the same question for one version. The
  edge-level functions stay: `sources_of` (the current version's edges only — an edge belongs to
  the version that consumed it, so this empties on the next edit), `sources_of_version`,
  `all_sources_of`, `derived_from`, `stale_derivations`. An edge stores
  `source_pgh_id` plus a denormalised `(source_obj_id, source_version)`, so "what has to be
  recomputed now that this changed" is one index scan. `Lineage` is not a `VersionedModel`: no version
  chain, append-only, never soft-deleted — but it carries `owner` and gets the tenant policy.
  There is deliberately no FK on the `*_pgh_id` columns (event rows are append-only, so a dangling
  pointer cannot arise, and an FK into history would make a *write* fail for a reference).
  Written by `POST /api/datasets/import-document` (`apps/datasets/api.py`), the one
  derivation in the app; any new one calls `record_derivation` the same way and gets the
  revision page's "Derived from" links for free.
- **Explorer** (`apps/core/explorer.py`, `/explorer/`): the same two structures as plain HTML,
  for when you do not yet know which object to ask about — users → models → rows (paged, with a
  date filter over `created`/`modified`, both in the query string) → one row's fields, revisions
  and both directions of its lineage → **one version** of that row (`…/<pk>/v3/`: what it held
  then, what changed in it, and the edges of that state alone) → **one edge**
  (`…/edge/<id>/`: the whole recorded call stack and the build it ran on). `…/jump/?id=`
  resolves any UUID to whatever holds it, across tenants and tables. Listings default to live
  rows, matching `Model.objects`, and say how many retired ones that hid (`?state=all` /
  `deleted`); a deleted row is marked in its own heading and at the far end of every lineage
  edge, where the version it was consumed at stays linked. Object and version are
  deliberately separate pages and each says which it is; every lineage link lands on a
  *version*, and an event row redirects to one rather than posing as an object of its own. Semantic HTML plus one stylesheet
  (`apps/core/static/explorer/explorer.css`), no build step. Staff session only
  and mounted only while `EXPLORER_ENABLED` (defaults to `DEBUG`), with the views checking again
  themselves; picking a user opens *their* tenant context, so RLS decides what it shows.
- **Revision page**: `GET /api/history/{resource}/{id}` (`apps/core/revisions.py`, `api.py`;
  `resource` is the model name lower-cased) → revisions grouped by context, diffed against the
  previous version, schema-aware, with lineage links. A group also carries the **child rows**
  written in the same save — any tracked owned model with a foreign key to the object, which is
  what an explicit m2m through model is (`revisions.child_relations`). Children are described
  (`is_related`, `description`: "added 'sales'"), not diffed: their columns are foreign keys, and
  UUIDs read worse than names. Frontend:
  `frontend/src/routes/history.$resource.$objectId.tsx`, linked from every list page.
- Tests: `apps/core/tests/test_history.py` (coverage, triggers, append-only, soft delete,
  schema log, lineage, the endpoint); event-table isolation lives in `test_tenancy.py`.
