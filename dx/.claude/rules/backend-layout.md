---
paths:
  - "**/backend/**"
---

## Backend (`backend/`)

- Python 3.14, Django 6.1, django-ninja, psycopg 3, pydantic-settings. Deps in `pyproject.toml`,
  lockfile `uv.lock`; add packages with `uv add <pkg>` / `uv add --group dev <pkg>`.
- Layout (see `NOTES.md` §9):
  - `config/` — `settings.py`, `urls.py`, `api.py` (root `NinjaAPI`, mount routers here),
    `env.py` (typed env via pydantic-settings — read config from `env`, never `os.environ`).
  - `apps/<name>/` — one Django app per feature: `api.py` (schemas, logic and the
    ninja `Router`), `models.py`, `admin.py`, `tests/test_*.py`, and an empty
    `management/commands/` ready for the app's own commands (see
    `.claude/rules/management-commands.md`). Register new apps in
    `INSTALLED_APPS` as `"apps.<name>"` and their router in `config/api.py`.
    **No `apps.py`**: Django synthesises the `AppConfig` from the `INSTALLED_APPS` entry (label =
    last component, `datasets`), so a stub that only restates `name` is a file to keep in sync
    for nothing. Add one only when the app needs `ready()` — Django picks it up
    automatically, no settings change. `apps/core/apps.py` is the one that earns it (system
    checks and the `connection_created` receiver).
  - `apps/core/` — infrastructure: `health.py` (`GET /api/health`, `GET /api/ready`, see
    `.claude/rules/health-checks.md`), `models.py` (`VersionedModel`: UUIDv7 pk + `created`/`modified` +
    `set_payload()`/`set_payload_partial()` for PUT/PATCH; `OwnedModel` + `OwnedManager`/
    `OwnedQuerySet.for_user()` — see "Data model conventions"), the multitenancy layer
    (`db.py` tenant context, `rls.py` policies, `middleware.py`, `checks.py`, `scrub.py`,
    `cache.py`, `testing.py` — see `.claude/rules/multitenancy.md`), the versioning layer (`history.py` capture +
    context + escape hatches, `lineage.py` the derivation graph, `revisions.py` the revision
    page's data layer, `explorer.py` the dev-only HTML browser over all three — see
    `.claude/rules/versioning.md`), `schemas.py`
    (`StrictSchema` for inputs), `examples.py` (`Model.example()` + `save_example()` — one
    saveable instance of every model, skill `model-examples`) + `save_deep.py` (write a
    hand-built tree of rows, children first — django-save-deep inlined for the lineage
    keywords),
    `backups.py` (see `.claude/rules/backups.md`), `scaffold.py` + `backend/scaffold/app/` (`manage.py
    newapp`), `cli.py` (the command index behind `manage.py tui`) + `usage.py` (`CommandRun`:
    every `manage.py` invocation, recorded from `manage.py` itself), `storage.py` (buckets), the sample Celery tasks + `tenant_task` (`tasks.py`,
    `/api/tasks/...` in `api.py`, tag `tasks`) and the cross-cutting tests
    (`tests/test_security.py`, `test_openapi.py`, `test_errors.py`, `test_ownership.py`,
    `test_tenancy.py`).
  - `apps/accounts/` — authentication, see `.claude/rules/auth.md` below.
  - `apps/datasets/` — demo feature app and the reference for new ones (`newapp`
    generates the same shape): `models.py` (`Dataset(OwnedModel)`, `DatasetId = NewType(...,
    uuid.UUID)`, `DatasetOptions` = typed JSON column, plus `Tag` and the explicit `DatasetTag`
    through model), `api.py` — the schemas (`DatasetOut`, `DatasetIn`, `DatasetPatch`), the
    logic and the router in one file. Views raise `HttpError` themselves and return
    `Status(201, obj)` for non-200 codes; the functions two routes share take the acting `user`
    first and carry a `_for` suffix where a route already owns the plain name (the route name is
    the operation id): `get_dataset_for`, `create_dataset_for`, `delete_dataset_for`,
    `import_dataset_for`. A lookup miss is a `HttpError(404, ...)` at the source, so there are no
    domain exception classes to map. A service with two callers that differ in lineage takes
    `operation=`/`sources=` as parameters (`create_dataset_for`, `create_note_for`): POST leaves
    the defaults, the import and the merge name their step and what they consumed. Then `tests/test_api.py`.
    **Tags** are the project's one many-to-many and its worked example of the rules that come
    with soft deletes: an owned, versioned join model; a conditional unique constraint so a
    retired name can be reused; matching case-insensitively while storing what the user typed;
    and cascade written out by hand (`set_dataset_tags`, `prune_unused_tags`) because
    Django's collector no longer runs. Read them through `dataset.tag_links` / `tag_names()` —
    `dataset.tags` is Django's m2m manager, which queries the join table raw and cannot see that
    a link was soft-deleted. Tags travel in `DatasetIn`/`DatasetPatch`, so one PATCH that renames
    a dataset and retags it is one revision spanning two tables.
    **`POST /api/datasets/import-document`** builds a dataset from an uploaded document
    (`import_dataset_from_document`) and is the one place that writes a lineage edge: the edge
    names the document *version* the rows were counted from, so a later rename does not rewrite
    history and `stale_derivations(document)` finds what needs rebuilding. `admin.py` is the
    standard three-line `register_all(models)` (see `.claude/rules/admin.md`); the admin is a dev tool and is
    not mounted in production (`ADMIN_ENABLED`).
    Endpoints: paginated `GET /api/datasets`, `POST /api/datasets`,
    `GET/PUT/PATCH/DELETE /api/datasets/{id}`.
  - `apps/documents/` — uploaded files and the **extraction snapshots** built from them
    (the design: `backend/documents_agent_brief.md` + `documents_model_v7.puml`; the
    module docstring of `models.py` lists what was adapted to this project's invariants).
    `models.py`: `Blob` (content-addressed bytes, per-tenant dedup by sha256, keys
    `documents/<owner id>/blobs/ab/cd/<sha256>`), `Extractor` (a strategy at a version),
    `Document` (title, meta, `source_blob`, `current_content` — the **facade**: `text`, `html`,
    `pages`, `outline()`, `hit()`, `confidence()`, all empty-safe), and the immutable snapshot
    `DocumentContent` (status, `is_current` with a partial unique index, the sanitized `html`,
    the plain `text` with a GIN full-text index) → `Page` → `Node` (one row per `data-nid` tag:
    materialized `path`, html/text offsets) → `PageRegion` (normalized polygon/envelope, word
    boxes with confidence; `conf_stats` rolled up bottom-up as `ConfStats`). `DocumentContent`,
    `Page` and `Node` also carry the **`Dated` mixin** — from when the content *originates*, not
    what it talks about (`dating.py` has the rule): `date_edtf` (EDTF, the truth), the strict
    `date_min`/`date_max` bounds (NULL = unknown on that side), `date_source` (explicit,
    metadata, inferred, interpolated, inherited, aggregated, curated) and `date_conf` (a
    per-estimate belief, never part of `conf_stats`). `UncertainDate` is the one EDTF ⇄ bounds
    implementation; `DatedQuerySet.overlapping(period)` the one period query, backed by the
    partial `(owner, date_min, date_max) WHERE is_current` index on contents (owner-leading:
    under RLS every query is tenant-scoped) and `(content, date_min)` on pages and nodes. The dating stage (`dating.date_snapshot`) runs inside the builder:
    datelines in blocks and headings → page envelopes → interpolation over page order (only
    while the anchors are chronological; a scrapbook is left with gaps) → aggregation up the
    tree → the content envelope (else the `Document.meta["date_hint"]` / file-metadata prior)
    → inheritance down; dated tags carry `data-date`. Re-dating is a rebuild from `raw_output`
    (`reextract(from_raw=True)`, `TreeStrategy.reproject`), a normal flip, no second lifecycle.
    `strategies.py`: the abstract `ExtractionStrategy` (`extract(document) -> DocumentContent`,
    with `self.snapshot(document, tree)` doing the rows), the built-ins `plain-text`, `html`,
    `pypdf`, and the opt-in `gemini-ocr` (**`ocr/`**: `page_schema.py` — the per-page block
    contract, pydantic + `google.genai` schema, the y-first `box_2d` → envelope conversion;
    `gemini_client.py` — the versioned prompt and `GeminiPageReader` with backoff and one
    repair retry; `render.py` — pdfium rasterization, one page in memory; `assembly.py` — the
    deterministic reduce: furniture off, cross-page merge, lists/figures grouped, the outline
    stack, one region per fragment; `run.py` — the resumable page loop; `preview.py` — the
    QA pages). `manage.py ocr extract|assemble|run` is the same core without a database, and
    `assemble` replays a production `raw_output` bit for bit. `conf_stats` stays NULL there
    ("no per-word confidence data available"); the model is never asked to rate itself.
    `?strategy=gemini-ocr` on `POST …/reextract` opts a document in — page images go to
    Google, so real records need a confirmed legal basis first. `extraction.py`: the neutral
    tree a strategy hands the builder + the parsers;
    `snapshot.py`: the write path — `store_blob`, `start_extraction` (a PENDING row + the
    `extract_content` task on commit), `run_extraction`, `write_snapshot` (plan → render →
    nh3 → measure offsets → rollups → one transaction), `switch_current` (the only writer of
    `is_current`/`current_content`, sends `content_switched`), `verify_snapshot`; `ops.py` +
    the commands `prune_contents` / `gc_blobs` (soft deletes; files stay). `api.py`: multipart
    `POST /api/documents/upload` (blob + document + a queued extraction per file), paginated
    list/get/PATCH/delete, `GET …/download` (signed link names the owner; the view opens
    their `tenant_context`), and the facade: `GET …/content`, `…/pages/{n}`, `…/hit`,
    `…/timeline` (dated nodes; `?source=`/`?max_conf=` make it a review queue),
    `…/extractions`, `POST …/reextract[?from_raw=true]`, `GET /api/documents/search?q=`,
    `GET /api/documents?period=<EDTF>` (the corpus query). Every response carries the
    information-origin date as `{edtf, min, max, source, conf, display}`. The admin shows
    `Blob` and the snapshot tables read-only (`admin.py`).
  - `apps/gallery/` — images and videos shown inline: `MediaItem` (owned; `kind` image|video,
    `file` under `gallery/<owner id>/%Y/%m/`), `api.py` decides the MIME type (`media_type_of`:
    the browser's, else guessed from the name), rejects everything that is not
    `image/*`/`video/*` and enforces per-kind size limits (`MAX_SIZE`). `POST /api/gallery/upload`, paginated `GET /api/gallery`, get/delete.
    `MediaItemOut.url` is the signed `/media/…` link (see `.claude/rules/media-storage.md`) — the SPA puts it
    straight into `<img src>` / `<video src>`.
  - `config/spa.py` + `config/static.py` — serve the built SPA (see `.claude/rules/spa-serving.md`);
    `config/media.py` — storage classes + `/media/<key>?sig=` view (see `.claude/rules/media-storage.md`).
  - `config/errors.py` — JSON error bodies (`{"detail": …}`) for API clients: Django's
    handler400/403/404/500 (`urls.py`) and a ninja exception handler that, with `DEBUG=true`,
    returns `{"detail": "Type: msg", "traceback": [...]}` for unhandled exceptions (in prod
    they propagate to Django's logging + `handler500`).
- Config: defaults in `config/env.py` target the compose database as the runtime role
  (`postgres://app_user:app_user@localhost:5432/dx`; `DB_ROLE`, `DB_MIGRATOR_*`, `DB_ADMIN_*`
  pick other roles — see `.claude/rules/multitenancy.md`). Override via env vars or `backend/.env`
  (template: `backend/.env.example`; `.env` is git-ignored). Other keys:
  `ACCESS_TOKEN_LIFETIME_MINUTES`, `REFRESH_TOKEN_LIFETIME_DAYS`, `API_FIXED_TOKEN`, `REGISTRATION_OPEN`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS` (JSON
  lists, only needed for the Capacitor origins), `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`,
  `CELERY_EAGER`, `MEDIA_STORAGE` + `S3_*` (see `.claude/rules/media-storage.md`), `S3_BACKUP_BUCKET` +
  `BACKUP_KEEP` (see `.claude/rules/backups.md`), `LOG_LEVEL`/`LOG_FORMAT`/`LOG_SQL` (see `.claude/rules/logging.md`). In containers
  every key can also be a file `/run/secrets/<KEY>` (pydantic-settings `secrets_dir`, only when
  the directory exists). Production keys — `HTTPS_ONLY`,
  `SECURE_HSTS_SECONDS`, `SECRET_KEY_FALLBACKS`, `CACHE_URL`, `DB_CONN_MAX_AGE`, `EMAIL_URL`,
  `DEFAULT_FROM_EMAIL`, `SENTRY_DSN`, `APP_VERSION` — are explained under "Production".
  `Env` refuses the dev `SECRET_KEY` when `DEBUG=false` (`production_guards`).
- Background tasks: Celery, see `.claude/rules/celery.md` below.
- Tests: see `.claude/rules/testing.md` below.
- Typing: mypy `strict` + the django-stubs plugin + the logic checks listed under "Type
  checking" (`./scripts/check.py backend`); ruff enforces annotations (`ANN`). Type everything;
  `# type: ignore[code]` needs the error code and a reason and is checked by
  `warn_unused_ignores`. `django_stubs_ext.monkeypatch()` runs in settings so generics like
  `ModelAdmin[Dataset]` work at runtime.
- Remove one again: `DB_ROLE=migrator uv run python manage.py deleteapp <name>` — the exact
  opposite, in the order that keeps the repo and the database in step: `migrate <name> zero`
  (its tables, its event tables and their policies, while the migration files still exist),
  then the two registrations, then `apps/<name>/`, then `history_schema --write`. It asks
  first, refuses `core`/`accounts`, and `--keep-data` removes the code only. Then
  `./scripts/sync_schema.sh`, `RESOURCES` in `test_ownership.py`, and the frontend route.
- New feature app: `uv run python manage.py newapp <name> [--model Item]` scaffolds
  `apps/<name>/` from `backend/scaffold/app/` (owned model with `name`/`description`, and one
  `api.py` with the Out/In/Patch schemas, a `get_<model>_for` lookup and a router with paginated
  list + get/POST/PUT/PATCH/DELETE; plus admin and tests), inserts it at the `# needle:` comments
  in `settings.py` (`INSTALLED_APPS`) and `config/api.py`, runs `makemigrations` + ruff. Then:
  adjust `models.py`/`api.py`, `./scripts/migrate.sh` (migrations + the RLS policy for the
  new table *and its event table*), `manage.py history_schema --write` after any field change,
  keep the model's `example()` in step with its fields (`manage.py check_examples`),
  add the resource to `RESOURCES` in `apps/core/tests/test_ownership.py`,
  `./scripts/sync_schema.sh` (view names become hook names), frontend route + nav entry. A new
  app is a tenant app automatically (`TENANT_APPS`); `test_tenancy.py` covers its model
  without registration. Logic in `apps/core/scaffold.py` (tested on temp copies).
