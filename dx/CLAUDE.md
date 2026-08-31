# dx — project guide for Claude

Data-management app: Django backend + React SPA, later wrapped with Capacitor for Android/iOS.
Architecture decisions and rationale live in `NOTES.md` (currently German; the decisions there are binding).

## Conventions

- **Everything in English**: code, comments, docs, commit messages.
- Monorepo: `backend/` (Django, uv) and `frontend/` (Vite SPA, pnpm). One deployable in prod
  (Django serves `frontend/dist` via WhiteNoise); in dev both run separately.
- **One module per feature: `apps/<feature>/api.py`** holds the ninja schemas, the logic and the
  router together. No `services.py`, no `schemas.py` — a lookup that 404s is three lines in the
  view, and the handful of functions two routes share sit above them in the same file.
- Every API operation declares `response=Schema`; never return bare dicts. The OpenAPI spec
  (`openschema.json`) is the contract with the frontend; the frontend talks to the API ONLY through
  the code orval generates from it (`frontend/src/api/`). See "End-to-end types".
- Decisions listed in `NOTES.md` §2 (stack table) are settled — do not swap libraries without asking.
- **Multitenancy, tenant == user**: every feature model is an `OwnedModel`; isolation is enforced
  by the ORM scope *and* by Postgres row-level security, never by hand-written filters alone.
  See "Multitenancy" — its invariants are not negotiable.
- **Everything is versioned and nothing is deleted**: every write to a feature model is mirrored
  into an append-only event table by a database trigger, and deletes are soft. See
  "Versioning, history and lineage" — those invariants are not negotiable either.

## Prerequisites

- `uv` (manages Python 3.14 and `backend/.venv` automatically)
- Docker with Compose v2 (dev Postgres)
- Node 24 + `pnpm` (installed via nvm: `npm install -g pnpm`). nvm is lazy-loaded in interactive
  shells only, so scripts and non-interactive shells must source `~/.nvm/nvm.sh` first
  (`./scripts/frontend.sh` does this).

## Daily commands

| Task                 | Command                                                     |
|----------------------|-------------------------------------------------------------|
| Start dev Postgres+Redis+S3 | `./scripts/db.sh` (`down`, `logs -f`, … are passed through); creates the media bucket |
| S3 console          | http://localhost:9101 (dx / dxdxdxdx) · `manage.py ensure_bucket` (media + backup bucket, idempotent) |
| Celery worker (alone)| `./scripts/celery.sh` (auto-reloads like runserver; `worker` = no reload, `beat`, `flower`, `purge`, `ping`); `serve.sh` already starts it — use this to run it separately; log: `logs/celery.log` |
| Run Django + worker  | `./scripts/serve.sh` (Django :8000 **and** the Celery worker in one terminal; `PORT=…`, `WORKER=0` = Django only); logs: `logs/backend.log`, `logs/celery.log` |
| Run Vite (frontend)  | `./scripts/frontend.sh` (= `frontend/scripts/serve.sh`) → http://localhost:5173; log: `logs/frontend.log` |
| Read dev server logs | `tail -f logs/backend.log` / `tail -f logs/frontend.log` (or `tail -n 100 …`); see "Dev server logs" |
| Migrate + RLS policies | `./scripts/migrate.sh [migrate args]` (= `DB_ROLE=migrator manage.py migrate` + `rls_sync` + `rls_sync --check`; plain `manage.py migrate` fails: the default DB role owns nothing) |
| Django management    | `cd backend && uv run python manage.py <cmd>`               |
| Tenant shell         | `cd backend && uv run python manage.py shell_as [-u USER \| --last]` (one user's data; picker with MRU + Tab completion) · `shell_admin --reason '…'` (all tenants, needs `DB_ADMIN_*`, audited) |
| Pull one tenant      | `manage.py pull_tenant USER [-o FILE] [--no-scrub]` → scrubbed fixture · `manage.py load_tenant FILE`; see "Multitenancy" |
| Erase one tenant     | `DB_ROLE=migrator manage.py delete_tenant USER [-y]` (user + all owned rows + **their version history and lineage** + their files; the admin's user deletion is disabled on purpose) |
| Tracked-field snapshot | `cd backend && uv run python manage.py history_schema [--write]` → `backend/history_schema.json`; run after any field change on a tracked model (see "Versioning") |
| Maintenance worker   | `./scripts/celery.sh maintenance` (beat + `maintenance` queue as the table owner; the nightly backup runs here, not on the dev worker) |
| Backend tests        | `cd backend && uv run pytest`                               |
| Backend lint/format  | `cd backend && uv run ruff check . && uv run ruff format .` |
| Backend type-check   | `cd backend && uv run mypy .` (strict + django-stubs)       |
| API docs / spec      | http://127.0.0.1:8000/api/docs · `/api/openapi.json`        |
| Health / readiness   | `curl http://127.0.0.1:8000/api/health` (liveness) · `/api/ready` (503 + failing checks; see "Health checks") |
| Frontend lint/format | `cd frontend && pnpm lint` / `pnpm format`                  |
| Frontend build       | `cd frontend && pnpm build` (runs `tsc -b` first)           |
| Add shadcn component | `cd frontend && pnpm dlx shadcn@latest add <name>`          |
| Sync schema + client | `./scripts/sync_schema.sh` = `frontend/sync_schema.sh` (`--check`, `--watch`) |
| Dev superuser        | `cd backend && uv run python manage.py createadmin` (admin/admin) |
| Token for curl       | `TOKEN=$(cd backend && uv run python manage.py token)` then `curl -H "Authorization: Bearer $TOKEN" …` (expires after `ACCESS_TOKEN_LIFETIME_MINUTES`; `-m 60` for longer) |
| Backend tests (short)| `./scripts/test.sh [pytest args]` · `./scripts/coverage.sh [--open]` |
| Format everything    | `./scripts/format.sh` (ruff + Biome, auto-fix)               |
| Lint everything      | `./scripts/lint.sh` (ruff, mypy, Biome — no changes)         |
| Type-check everything| `./scripts/check.py [backend\|frontend]` (mypy strict + django-stubs, `tsc -b`); see "Type checking" |
| CI in one command    | `./scripts/ci.py [backend\|frontend\|image]` = `check.py` + `./scripts/build.sh` (production image `dx-app:latest`) |
| Full pre-commit check| `./scripts/check.sh` (lint + pytest + build + sync_schema --check) |
| DB backup / restore  | `./scripts/backup.sh [--list\|--prune]` (= `DB_ROLE=migrator manage.py backup` → `dx-backups` bucket or `backend/backups/`), `./scripts/restore.sh <name>\|--latest [-y]`, `./scripts/roundtrip.sh` (dev only, drops the DB); see "Backups" |
| New feature module   | `cd backend && uv run python manage.py startmodule <name> [--model Item]` (scaffold + register + makemigrations; see "Backend") |
| Versioning/lineage demo | `/notes` in the SPA → edit a note, merge two, then "History" / "Lineage" (`apps/notes`, a showcase — see its section) |
| Reference command    | `cd backend && uv run python manage.py hello_world [NAME] --shout` (django-click + rich; see "Management commands") |
| Build production image | `./scripts/build.sh` → `dx-app:latest` (`--run` also starts it on :8080 via the dev compose `app` profile, plain http) |
| Production stack     | `./scripts/prod.sh` (= `docker compose --env-file docker/.env.prod -f docker/docker-compose.prod.yml …`; no args = `up -d --wait`); see "Production" |
| Deployment checklist | `cd backend && DEBUG=false SECRET_KEY=… ALLOWED_HOSTS='["…"]' EMAIL_URL=… uv run python manage.py check --deploy` (the entrypoint runs this) |

**Dev server logs:** both serve scripts (`tee`) write everything they print to stdout *and* to
`logs/backend.log` / `logs/frontend.log` (repo root, git-ignored). The file is truncated on every
start, so it only ever contains the current run. **To get the server output, tail these files —
do not start a second server or ask the user to paste terminal output:**

- `tail -f logs/backend.log` / `tail -f logs/frontend.log` — follow live while reproducing something
  (e.g. run a `curl` against the API, then read what Django printed).
- `tail -n 100 logs/backend.log` — the most recent lines; `grep -n Traceback logs/backend.log` —
  jump to errors.

`backend.log` holds Django request lines and tracebacks (`PYTHONUNBUFFERED=1`, so lines appear
immediately); `frontend.log` holds Vite/TypeScript/HMR errors (`--clearScreen false`, so no terminal
escape sequences); `celery.log` (`./scripts/celery.sh`, also `worker`; `beat` → `celery-beat.log`)
holds the worker's task log — "Task … received/succeeded", task tracebacks, and the watchfiles
restart lines. An empty or missing file means that process was not started via its script.

**After every change — backend or frontend — run the checkers on both sides before reporting it
done.** Type checkers first: `./scripts/check.py` (mypy + `tsc -b`; always both — a backend schema
change breaks frontend types and vice versa). Then the changed side's lint and tests: backend
`ruff check`, `ruff format --check`, `pytest`; frontend `pnpm lint`, `pnpm build`.
`./scripts/check.sh` runs all of that plus `sync_schema.sh --check` in one go and is the safe
default. Fix what they report; never call work finished with a red checker.
**Formatting is automatic, never by hand**: after editing run `./scripts/format.sh` (backend:
`ruff check --fix` + `ruff format`; frontend: `biome check --write`) — ruff and Biome are the
formatters, `ruff format --check` / `pnpm lint` only confirm the result.
After changing any ninja schema/endpoint run `./scripts/sync_schema.sh` and commit `openschema.json` +
`frontend/src/api/` (pytest and `sync_schema.sh --check` fail otherwise).

## End-to-end types (NOTES.md §4)

Pipeline: ninja/Pydantic schemas → `openschema.json` (repo root, committed) → orval
(`orval.config.ts`, repo root) → `frontend/src/api/` (committed, never edited).

- `./scripts/sync_schema.sh` runs `manage.py export_openapi_schema` (needs `"ninja"` in
  `INSTALLED_APPS`) and then orval; the orval hook formats output with Biome so regeneration is
  idempotent. `--check` regenerates and fails on any diff (use in CI); `--watch` re-runs on backend
  file changes (`watchfiles`). It needs **no running server and no database** — the spec is built
  in-process from `config.api.api` (verified with an unreachable `DATABASE_URL`), so it works in
  CI and on a fresh checkout after `uv sync` + `pnpm install`.
- Generated layout: `src/api/<tag>/<tag>.ts` (fetchers like `listDatasets()`, hooks like
  `useListDatasets()`, `useCreateDataset()` (mutation, variables `{ data }`),
  `useDeleteDataset()` (variables `{ datasetId }`), key helpers `getListDatasetsQueryKey()`),
  `src/api/model/` (TS types `DatasetOut`, `DatasetIn`, …), `src/api/zod/<tag>/<tag>.ts`
  (`CreateDatasetBody`, `ListDatasetsResponse`, … for forms/validation).
- Transport: every generated call goes through `src/lib/custom-fetch.ts` (`customFetch`, orval
  `mutator`): prefixes `VITE_API_URL` (empty on web, absolute for native builds), parses JSON,
  throws `ApiError(status, detail)` on non-2xx so TanStack Query gets an error state. Use
  `errorMessage(error)` to display errors (orval types errors as `unknown`).
- Hook/function names come from operation ids = the ninja view function names
  (`config/api.py::Api.get_openapi_operation_id`), so view names must be unique across all
  routers and snake_case (`apps/core/tests/test_openapi.py` enforces it; `list_datasets` →
  `useListDatasets`). Router `tags=[...]` decide the output folder.
- ninja `ModelSchema` gotcha: primary keys, `blank=True` fields and fields with defaults come out
  optional/nullable in the spec (→ `id?: number | null`). Redeclare them in the schema body
  (`id: int`) to keep the TS types strict — see `DatasetOut`.
- Multipart: declare `files: File[list[UploadedFile]]` (ninja's generic annotation style — mypy
  rejects the `= File(...)` default form). orval then generates a `FormData` builder and a
  `{ files: Blob[] }` body type; pass browser `File[]` (`useUploadDocuments().mutate({ data: { files } })`).
  Binary responses (`FileResponse`) are not part of the JSON contract — link to them via the URL
  the API returns (`download_url`), not via the generated fetcher.
- Rule for frontend code: no hand-written fetch calls, URL strings, or API types. If something is
  missing, add it to the backend and regenerate.

## Backend (`backend/`)

- Python 3.14, Django 6.1, django-ninja, psycopg 3, pydantic-settings. Deps in `pyproject.toml`,
  lockfile `uv.lock`; add packages with `uv add <pkg>` / `uv add --group dev <pkg>`.
- Layout (see `NOTES.md` §9):
  - `config/` — `settings.py`, `urls.py`, `api.py` (root `NinjaAPI`, mount routers here),
    `env.py` (typed env via pydantic-settings — read config from `env`, never `os.environ`).
  - `apps/<feature>/` — one Django app per feature module: `api.py` (schemas, logic and the
    ninja `Router`), `models.py`, `admin.py`, `tests/test_*.py`. Register new apps in
    `INSTALLED_APPS` as `"apps.<feature>"` and their router in `config/api.py`.
  - `apps/core/` — infrastructure: `health.py` (`GET /api/health`, `GET /api/ready`, see "Health
    checks"), `models.py` (`BaseModel`: UUIDv7 pk + `created`/`modified` +
    `set_payload()`/`set_payload_partial()` for PUT/PATCH; `OwnedModel` + `OwnedManager`/
    `OwnedQuerySet.for_user()` — see "Data model conventions"), the multitenancy layer
    (`db.py` tenant context, `rls.py` policies, `middleware.py`, `checks.py`, `scrub.py`,
    `cache.py`, `testing.py` — see "Multitenancy"), the versioning layer (`history.py` capture +
    context + escape hatches, `lineage.py` the derivation graph, `revisions.py` the revision
    page's data layer — see "Versioning, history and lineage"), `schemas.py`
    (`StrictSchema` for inputs),
    `backups.py` (see "Backups"), `scaffold.py` + `backend/scaffold/module/` (`manage.py
    startmodule`), `storage.py` (buckets), the sample Celery tasks + `tenant_task` (`tasks.py`,
    `/api/tasks/...` in `api.py`, tag `tasks`) and the cross-cutting tests
    (`tests/test_security.py`, `test_openapi.py`, `test_errors.py`, `test_ownership.py`,
    `test_tenancy.py`).
  - `apps/accounts/` — authentication, see "Auth" below.
  - `apps/datasets/` — demo feature module and the reference for new ones (`startmodule`
    generates the same shape): `models.py` (`Dataset(OwnedModel)`, `DatasetId = NewType(...,
    uuid.UUID)`, `DatasetOptions` = typed JSON column, plus `Tag` and the explicit `DatasetTag`
    through model), `api.py` — the schemas (`DatasetOut`, `DatasetIn`, `DatasetPatch`), the
    logic and the router in one file. Views raise `HttpError` themselves and return
    `Status(201, obj)` for non-200 codes; the functions two routes share take the acting `user`
    first and carry a `_for` suffix where a route already owns the plain name (the route name is
    the operation id): `get_dataset_for`, `create_dataset_for`, `delete_dataset_for`,
    `import_dataset_for`. A lookup miss is a `HttpError(404, ...)` at the source, so there are no
    domain exception classes to map. Then `tests/test_api.py`.
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
    history and `stale_derivations(document)` finds what needs rebuilding. `admin.py` registers
    the three models with `OwnedModelAdmin` (see "Admin"); the admin is a dev/staff tool and is
    not mounted in production (`ADMIN_ENABLED`).
    Endpoints: paginated `GET /api/datasets`, `POST /api/datasets`,
    `GET/PUT/PATCH/DELETE /api/datasets/{id}`.
  - `apps/documents/` — file uploads: `Document(OwnedModel)` (`FileField` on Django's default
    storage = the S3-compatible object store, see "File storage"; keys
    `documents/<owner id>/%Y/%m/<name>`, `owned_upload_path`),
    multipart `POST /api/documents/upload` (`files: File[list[UploadedFile]]`, validated as a
    batch by `validate_upload`), paginated list/get/delete, and `GET /api/documents/{id}/download`
    (streams a `FileResponse`; exposed to clients as `DocumentOut.download_url`, so storage can
    change without touching the frontend; the signed link — which names the owner — replaces
    the user check: the view reads the row inside that owner's `tenant_context`).
  - `apps/gallery/` — images and videos shown inline: `MediaItem` (owned; `kind` image|video,
    `file` under `gallery/<owner id>/%Y/%m/`), `api.py` decides the MIME type (`media_type_of`:
    the browser's, else guessed from the name), rejects everything that is not
    `image/*`/`video/*` and enforces per-kind size limits (`MAX_SIZE`). `POST /api/gallery/upload`, paginated `GET /api/gallery`, get/delete.
    `MediaItemOut.url` is the signed `/media/…` link (see "Media files") — the SPA puts it
    straight into `<img src>` / `<video src>`.
  - `config/spa.py` + `config/static.py` — serve the built SPA (see "Serving the SPA");
    `config/media.py` — storage classes + `/media/<key>?sig=` view (see "Media files").
  - `config/errors.py` — JSON error bodies (`{"detail": …}`) for API clients: Django's
    handler400/403/404/500 (`urls.py`) and a ninja exception handler that, with `DEBUG=true`,
    returns `{"detail": "Type: msg", "traceback": [...]}` for unhandled exceptions (in prod
    they propagate to Django's logging + `handler500`).
- Config: defaults in `config/env.py` target the compose database as the runtime role
  (`postgres://app_user:app_user@localhost:5432/dx`; `DB_ROLE`, `DB_MIGRATOR_*`, `DB_ADMIN_*`
  pick other roles — see "Multitenancy"). Override via env vars or `backend/.env`
  (template: `backend/.env.example`; `.env` is git-ignored). Other keys:
  `ACCESS_TOKEN_LIFETIME_MINUTES`, `REFRESH_TOKEN_LIFETIME_DAYS`, `API_FIXED_TOKEN`, `REGISTRATION_OPEN`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS` (JSON
  lists, only needed for the Capacitor origins), `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`,
  `CELERY_EAGER`, `MEDIA_STORAGE` + `S3_*` (see "File storage"), `S3_BACKUP_BUCKET` +
  `BACKUP_KEEP` (see "Backups"), `LOG_LEVEL`/`LOG_FORMAT`/`LOG_SQL` (see "Logging"). In containers
  every key can also be a file `/run/secrets/<KEY>` (pydantic-settings `secrets_dir`, only when
  the directory exists). Production keys — `HTTPS_ONLY`,
  `SECURE_HSTS_SECONDS`, `SECRET_KEY_FALLBACKS`, `CACHE_URL`, `DB_CONN_MAX_AGE`, `EMAIL_URL`,
  `DEFAULT_FROM_EMAIL`, `SENTRY_DSN`, `APP_VERSION` — are explained under "Production".
  `Env` refuses the dev `SECRET_KEY` when `DEBUG=false` (`production_guards`).
- Background tasks: Celery, see "Background tasks (Celery)" below.
- Tests: see "Testing" below.
- Typing: mypy `strict` + the django-stubs plugin + the logic checks listed under "Type
  checking" (`./scripts/check.py backend`); ruff enforces annotations (`ANN`). Type everything;
  `# type: ignore[code]` needs the error code and a reason and is checked by
  `warn_unused_ignores`. `django_stubs_ext.monkeypatch()` runs in settings so generics like
  `ModelAdmin[Dataset]` work at runtime.
- New feature module: `uv run python manage.py startmodule <name> [--model Item]` scaffolds
  `apps/<name>/` from `backend/scaffold/module/` (owned model with `name`/`description`, and one
  `api.py` with the Out/In/Patch schemas, a `get_<model>_for` lookup and a router with paginated
  list + get/POST/PUT/PATCH/DELETE; plus admin and tests), inserts it at the `# needle:` comments
  in `settings.py` (`INSTALLED_APPS`) and `config/api.py`, runs `makemigrations` + ruff. Then:
  adjust `models.py`/`api.py`, `./scripts/migrate.sh` (migrations + the RLS policy for the
  new table *and its event table*), `manage.py history_schema --write` after any field change,
  add the resource to `RESOURCES` in `apps/core/tests/test_ownership.py`,
  `./scripts/sync_schema.sh` (view names become hook names), frontend route + nav entry. A new
  module is a tenant app automatically (`TENANT_APPS`); `test_tenancy.py` covers its model
  without registration. Logic in `apps/core/scaffold.py` (tested on temp copies).

## `apps/notes` — showcase, safe to delete

A note-taking module that exists to *demonstrate* versioning and lineage end to end, not because
the product needs notes. It is deliberately self-contained.

- `Note(OwnedModel)` — title, body, and `tags` as a plain comma-separated string, normalised on
  write (`api.normalize_tags`) so that retyping the same set in a different order is not a
  change in the note's history. Deliberately *not* the shape `apps/datasets` uses: an owned,
  versioned `Tag` model with a join table is what you want when tags are shared, renamed or
  counted; a string is what you want when a tag is just a label on one note.
- One derivation, `merge` (many → one), recording an edge to the **version** of each source it
  read (`merge_notes_for` in `apps/notes/api.py`). Merging alone is enough for a directed acyclic
  *graph*: a note
  merged into two others branches, and merging two merged notes closes a diamond. The sources are
  left alone — a merge adds a note, it does not consume any.
- `GET /api/lineage/{resource}/{id}?depth=` (`apps/core/lineage.py::graph`, `apps/core/api.py`)
  walks that graph from any object, in **both** directions: a sibling and a co-parent are only
  reachable by going up and then down again. `depth` on a node is a signed generation (a source
  is -1, something derived +1, a sibling 0), which is what lets the page draw it in rows.
  The endpoint is generic — it works for datasets too, which have document→dataset edges.
- Frontend: `src/routes/notes.tsx` (create, edit in place, select-and-merge) and
  `src/routes/lineage.$resource.$objectId.tsx` (the graph as plain SVG — no layout library for
  one screen; a stale edge is dashed and red).

**To delete it**: remove `apps/notes/`, `frontend/src/routes/notes.tsx`, its nav entry in
`__root.tsx`, the `notes` entries in `INSTALLED_APPS` and `config/api.py`, the `notes` resource
in `apps/core/tests/test_ownership.py` and the two `notes.*` lines in `test_tenancy.py`'s erasure
and `TENANT_APPS` assertions; then `manage.py history_schema --write` and
`./scripts/sync_schema.sh`. Keep `apps/core/lineage.py::graph` and
`/api/lineage/...` — those are general infrastructure, not part of the showcase.

## Multitenancy (tenant == user; `apps/core/db.py`, `rls.py`, `middleware.py`, `checks.py`)

One database, one schema, row-level isolation enforced **twice** — an application bug must not
be able to leak data:

| Layer | Where | Role |
|---|---|---|
| Data model | `OwnedModel.owner` FK (the guide's `TenantModel`) | scoping and per-tenant extraction are mechanical |
| ORM guard | `OwnedManager` on django-scopes' state (`scope`, `scopes_disabled`) | a query outside a tenant scope **raises** `ScopeError`; inside it is filtered |
| **Database guard** | **Postgres row-level security**, policy `tenant_isolation` on every table with an `owner_id` column — owned models, the event tables holding their history, and `Lineage` (`apps/core/rls.py::isolated_models`, `manage.py rls_sync`) | **the guarantee**: `owner_id = NULLIF(current_setting('app.user_id', true), '')::uuid` for `USING` and `WITH CHECK`; no context → nothing visible or writable (fails closed) |
| Request context | `TenantMiddleware`: bearer token verified once → `SET LOCAL app.user_id` + ORM scope inside one transaction (`tenant_context(user_id)`) | stateless, safe with connection reuse / PgBouncer transaction pooling |
| Roles | `app_migrator` (owns tables; migrate, rls_sync, backups), `app_user` (runtime; RLS applies), `app_admin` (`BYPASSRLS`; `shell_admin`) — `docker/postgres/10-roles.sh` | RLS only binds roles that are neither owner, superuser nor `BYPASSRLS` |

Rejected (do not revisit; NOTES.md §11): schema-per-tenant / database-per-tenant (10k tenants ×
DDL, PgBouncer-incompatible session state, header routing = privilege escalation), manual
`.filter(owner=…)` as the only guard, per-object ACLs (`django-guardian`: opt-in, we need
default-deny).

- **Tenant context** (`apps/core/db.py`): `tenant_context(user_id)` = `tenant_db_context`
  (`atomic()` + `SET LOCAL`, previous value restored on exit) + `scope(user=user_id)`; also sets
  the `current_user_id` contextvar. Requests: the middleware (only under `/api/`, only from a
  valid `Authorization: Bearer`; `BearerAuth` reuses that verification instead of decoding
  twice — a session cookie never authenticates the API). Tasks: `@tenant_task` (below). Tests:
  `apps.core.testing.acting_as(user)`. Session-level `pin_session_tenant()` /
  `unpin_session_tenant()` (re-applied on reconnect, process-wide) only where one job owns the
  connection — commands, shells, the worker side of a `tenant_task`; never in a request. `scope()` takes the
  user's **pk (UUID)**, not the object. Streaming responses are served after the transaction
  ended: materialise querysets before returning a `StreamingHttpResponse` (the middleware logs
  `tenant_streaming_response`).
- **Models**: every concrete model in a tenant app (`TENANT_APPS` = every `apps.*` app except
  `SHARED_APPS = ["apps.core", "apps.accounts"]`) inherits `OwnedModel`, or is listed in
  `SHARED_MODELS` after review — system check `tenant.E001`; an auto-created M2M through table
  is `tenant.E002` (declare an owned `through=` model). `owner` is `editable=False`, filled from
  the context by `save()` when a service does not pass it (`NoTenantContext` otherwise),
  `on_delete=CASCADE`, indexed as `(owner, -created, -id)` (the index name is left to Django:
  `Index.max_name_length` is 30, so a literal `%(app_label)s_%(class)s…` pattern in the abstract
  base would break `makemigrations` for any longer module name). `bulk_create` skips `save()`,
  so pass `owner=` explicitly there — the policy rejects the row otherwise, NULL owner included.
  `User`, `ApiToken`, `RefreshToken` are shared tables:
  no RLS, no scope (authentication runs before any context exists). There is no user list or
  lookup endpoint besides `/api/auth/me` — do not add one.
- **Policies** are generated from the model registry, never written by hand:
  `manage.py rls_sync` (DB_ROLE=migrator) enables RLS and (re)creates the policy on every table
  whose state is not already right. The set is `rls.isolated_models()` — **anything with an
  `owner_id` column**, not just `OwnedModel` subclasses: an event table holds the same rows one
  version older and `Lineage` holds tenant data without being versioned, so a rule keyed on the
  base class would have let both escape (`app_user` can read every table; RLS is what stops it).
  A rollback that drops an `owner` column fails while the policy references it — `DROP POLICY
  tenant_isolation ON <table>` first, then migrate backwards; the next `rls_sync` restores it — a healthy database sees no DDL and therefore no
  locks, which matters because it runs on every container start. `rls_sync --check` /
  `rls.verify()` lists drift: table missing, RLS off, policy missing, policy for the wrong
  role, **policy expression different from the generated one** (a hand-altered `USING (true)`
  or a leftover from an earlier `TENANT_GUC` is drift, not health — `rls.expected_expression()`
  is pinned by a test against a live policy), and missing write privileges (all four are
  checked separately: `has_table_privilege(role, table, 'SELECT, INSERT, …')` is OR, not AND).
 Deploy sequence, everywhere: `migrate` → `rls_sync` → `rls_sync
  --check` (`./scripts/migrate.sh`, `docker/entrypoint.sh`). Also: `/api/ready` check `rls`
  (drift, or the process connects as a role that bypasses RLS → 503) and `manage.py check
  --database default` (`tenant.E003`, skipped while migrations are pending). Never add
  `OR current_setting(...) IS NULL` to a policy.
- **Roles / DB_ROLE** (`config/env.py`): `DATABASE_URL` carries the runtime credentials
  (`app_user`); `DB_ROLE=migrator` switches to `DB_MIGRATOR_USER/PASSWORD` (dev default: the
  compose superuser `dx`; prod: `app_migrator`), `DB_ROLE=admin` to `DB_ADMIN_USER/PASSWORD`
  (no default; dev: `app_admin`/`app_admin` in `backend/.env`; never in the web/worker
  environment). `./scripts/db.sh` runs `docker/postgres/10-roles.sh` (idempotent: creates the
  roles, hands tables to `app_migrator`, grants). Invariants: `app_user` never owns a table, is
  never a superuser, never `BYPASSRLS`.
- **Tasks**: `@tenant_task(...)` (`apps/core/tasks.py`) is the only decorator allowed in
  tenant apps (`test_tenancy.py` greps for `@shared_task`); the function takes `owner_id:
  uuid.UUID` first, ids are passed — never instances. Worker: a *pinned* session context
  (`pin_session_tenant`, re-applied by the `connection_created` receiver, so a reconnect
  mid-task cannot silently drop the tenant and make the task succeed over an empty database),
  cleared again when the task returns; it is process-wide state, so one job at a time per
  process (Celery's prefork pool). Eager mode: `tenant_context`. A task that reads rows the
  request just wrote must be enqueued in `transaction.on_commit(...)`: the middleware's
  transaction is still open when the view returns. Cross-tenant work (the nightly backup) is
  routed to the `maintenance` queue (`CELERY_TASK_ROUTES`), consumed only by the maintenance
  worker running as the table owner (`./scripts/celery.sh maintenance`, prod `beat` service).
- **Tooling**: `manage.py shell_as` (one user, both layers pinned; subclasses Django's `shell`,
  so `-c`, `-i ipython` work), `shell_admin --reason '…'` (`app_admin`, scopes off, audit line
  `shell_admin_opened` on logger `tenant.admin_access`), `pull_tenant USER` (one user's rows as
  a scrubbed fixture — structurally cannot contain another tenant: runs as `app_user`),
  `load_tenant FILE` (pins the fixture's user), `delete_tenant USER` (erasure: every owned row,
  **its version history and lineage edges**, and the files they reference — rows in one
  transaction and files only after it commits; `apps/core/tenants.py`). Both tools read through
  `tenants.owned_rows()` (`_base_manager`), so soft-deleted rows are exported and erased too: an
  export that hid them would be a false answer to "what do you hold about me", and history that
  outlived an erased user would defeat the erasure. Erasure is the one caller allowed to really
  delete (`history.hard_delete()`). Erasure needs cross-tenant credentials, and that is also why
  **`UserAdmin` refuses to delete**: outside a tenant context the cascade cannot see the owned
  rows, so the admin would report success and then fail at the foreign key on commit. `apps/core/scrub.py`: `PII_FIELDS` allowlist —
  every model field with such a name needs a `SCRUBBERS` entry or the export refuses
  (`check_scrubbers()` is a test). Backups (`dumpdata`) need cross-tenant access and refuse
  the runtime role (`CrossTenantAccessRequired`). Connect to Postgres directly (not via a
  transaction-pooling PgBouncer) for the shells: session variables do not survive pooling.
- **Known trade-offs of the middleware transaction**: the whole view runs inside it, so a
  multi-file upload holds a database connection while the bytes go to the object store (fine at
  this size; watch `idle in transaction` if uploads grow), and anything enqueued during a
  request is published before that transaction commits (`transaction.on_commit`, above).
  A `StreamingHttpResponse` is produced *after* the context closes: file streams are fine (no
  ORM), lazy querysets are not — materialise them (the middleware logs
  `tenant_streaming_response` when it sees one).
- **Peripheral guardrails**: cache keys via `apps.core.cache.tenant_cache_key()` (raw
  `cache.get/set` in tenant apps fails a test); file keys carry the owner id
  (`owned_upload_path`), links stay signed; the Django admin registers owned models but only
  inside a tenant context it opens itself (`AdminTenantMiddleware`, see "Admin") and is off in
  production; 404 not 403; UUIDs in URLs;
  `ModelForm`s on owned models would need `django_scopes.forms.SafeModelChoiceField`;
  "generate until globally unique" helpers and `validate_unique` must run in
  `scopes_disabled()`; forward FK traversal (`obj.related`) uses `_base_manager` (unscoped —
  RLS covers it, which is why RLS is the guarantee); FK constraint checks bypass RLS, so an
  FK violation can confirm that another tenant's row exists — accepted as a low-severity
  existence oracle (decision 2026-08-29).
- **Tests** (`conftest.py`): the suite connects as the migrator (it creates and migrates the
  test database, then `rls.sync()`), and every database test runs `SET ROLE app_user` so RLS is
  enforced exactly as in production; `@pytest.mark.cross_tenant` keeps the owner's view for
  backup/restore tooling. Direct service calls need `with acting_as(user):`; requests get the
  context from the middleware. `test_tenancy.py` loops over every owned model: scope + RLS
  isolation, fails-closed without context, `WITH CHECK` on foreign owners, middleware, tasks,
  scrubbers, the source-level rules. After adding a model: `pytest --create-db` once.

## Versioning, history and lineage (`apps/core/history.py`, `lineage.py`, `revisions.py`)

Every model keeps a full version history, normal queries see only current rows, and **nothing is
ever hard-deleted**. On top of the versions sits a lineage graph whose edges point at a *specific
version* of a source, not at the live row.

| Piece | Where | What it guarantees |
|---|---|---|
| Capture | django-pghistory triggers, `@tracked` (`apps/core/history.py`) | a `.update()`, a `bulk_update`, raw SQL and a data migration all produce version rows — application code cannot bypass it |
| Storage | one event table per model (`DatasetEvent`, …), typed mirrored columns | history is queryable per field, and migrating it is a normal migration |
| Immutability | `PGHISTORY_APPEND_ONLY` → pgtrigger rejects UPDATE and DELETE on event tables | lineage nodes cannot be edited or vanish |
| Version chain | `BaseModel.version`, bumped by the `bump_version` trigger | authoritative ordering; "which version came first" does not rest on clock behaviour |
| Soft delete | `BaseModel.deleted_at` + `pgtrigger.Protect` on DELETE | a deleted row stays resolvable, so old lineage edges keep working |
| Isolation | the same RLS policy on every event table and on `Lineage` | history is tenant data (see "Multitenancy") |

- **Tracking a model**: `@tracked` from `apps/core/history.py`, on the concrete model. Never on an
  abstract base — pghistory accepts the decorator there and then generates one concrete event
  model pointing at the abstract class (`fields.E300`) while installing **no triggers at all** on
  the subclasses. `startmodule` writes `@tracked` for you. Opting out means adding the label to
  `HISTORY_EXEMPT` with a reason (currently the two `accounts` token models: high-churn
  credential bookkeeping with no lineage value). `test_history.py` fails on anything untracked
  and unlisted, and checks what the event model actually tracks rather than
  `hasattr(pgh_event_model)` — that attribute is inherited.
- **Reading history**: `obj.events`, or `history.event_rows(Model, pk)` / `history.current_event(obj)`
  which return rows typed as `history.EventRow` (event models are generated at import time, so a
  protocol is the only way to describe them to mypy). `Model.pgh_event_model` is the class.
- **Never assign `id`, `created`, `modified` or `version` in Python** — all four are database
  defaults or trigger-set (`apps/core/models.py::BaseModel`). `Now()` renders as
  `STATEMENT_TIMESTAMP()` on Postgres, which is why the trigger uses it too; `NOW()` is the
  transaction start and would put `modified` *before* `created`. `save()` reads `version` and
  `modified` back afterwards, because the instance it returns is what the API serialises — an
  INSERT needs no such read (Django fetches database defaults with RETURNING). A queryset
  `.update()` bypasses all of this by design: call `refresh_from_db()` if you need the values.
- **Deleting**: `obj.soft_delete()`, or a service that calls it. `Model.objects` hides
  soft-deleted rows, `Model.all_objects` includes them, and Django's `_base_manager` (forward FK
  traversal, `refresh_from_db`) is deliberately left unfiltered so `document.owner` still
  resolves after the owner was soft-deleted. Direct `.delete()` raises in the database.
  Uploaded files are **not** removed on delete — earlier versions still reference them; only
  tenant erasure reclaims them.
- **Every unique constraint is conditioned on `deleted_at__isnull=True`** (see
  `accounts.ApiToken`), otherwise a soft-deleted row reserves its value forever. `unique=True`
  and `unique_together` on a `BaseModel` fail a test.
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
  or their rows are attributed to nothing. **`pghistory_context` is a single shared table every
  tenant can read** — its upsert function needs SELECT and UPDATE on it, so RLS cannot hide it.
  Nothing tenant-identifying goes in the metadata: no user id (the event row's `owner_id` already
  says whose change it was) and no resolved URL (`/api/datasets/<uuid>` names another tenant's
  object). `history_context` raises on a value that looks like an identifier. This is why we do
  **not** use pghistory's own `HistoryMiddleware`, which records both.
- **Lineage** (`apps/core/lineage.py`): `record_derivation(target, sources=[...])` inside the
  writing transaction; `sources_of`, `derived_from`, `stale_derivations`. An edge stores
  `source_pgh_id` plus a denormalised `(source_obj_id, source_version)`, so "what has to be
  recomputed now that this changed" is one index scan. `Lineage` is not a `BaseModel`: no version
  chain, append-only, never soft-deleted — but it carries `owner` and gets the tenant policy.
  There is deliberately no FK on the `*_pgh_id` columns (event rows are append-only, so a dangling
  pointer cannot arise, and an FK into history would make a *write* fail for a reference).
  Written by `POST /api/datasets/import-document` (`apps/datasets/api.py`), the one
  derivation in the app; any new one calls `record_derivation` the same way and gets the
  revision page's "Derived from" links for free.
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

## Admin (`apps/core/admin.py`, `admin_lineage.py`)

Version history and lineage, browsable — without the admin becoming a way to hard-delete rows,
edit history, or read another tenant's data. **Not mounted in production**: `ADMIN_ENABLED`
defaults to `DEBUG`, and with it off `/admin/` does not resolve and the interactive API docs go
with it (they need an admin session to log in with). Administer production through
`manage.py shell_as` / `shell_admin`.

- **`AdminTenantMiddleware` gives an admin request a tenant context**, resolved from the admin
  session (tenant == user, so a staff user browsing the admin is a tenant browsing their own
  rows). Without it every owned-model page is empty and `OwnedManager` raises: `TenantMiddleware`
  only runs under `/api/`, so neither the ORM scope nor `app.user_id` would be set. Kept separate
  from `TenantMiddleware` on purpose — that one trusts a bearer token and nothing else, which is
  what stops a session cookie from ever authenticating the API.
- **Every `BaseModel` subclass registers with `BaseModelAdmin`** (owned models with
  `OwnedModelAdmin`, which adds the lineage page), never a plain `ModelAdmin`. A plain one
  reintroduces the delete button and `delete_selected`, both of which issue a real `DELETE` and
  surface the `no_hard_delete` trigger as a 500. `apps/core/tests/test_admin.py` fails on any
  that does — including the two `accounts` token models, which are shared tables but still
  `BaseModel`s.
- **Admin `get_queryset` uses `all_objects`.** `objects` hides soft-deleted rows from the only
  interface that can restore them. `soft_delete_action` / `restore_action` replace the delete
  button; restore catches the `IntegrityError` from a partial unique index (a name freed by a
  delete can be taken again) and reports it as a message per row instead of a 500.
- **FKs to soft-deleted rows keep validating**: `formfield_for_foreignkey` swaps in
  `all_objects`, because admin choice fields query `_default_manager` and would otherwise fail a
  save nobody asked for with "Select a valid choice". New FKs between `BaseModel` subclasses
  need no extra work.
- **Event admins subclass `ReadOnlyEventAdmin`** and refuse add/change/delete at the permission
  layer — the tables are append-only in the database, so an edit surfaces as a trigger error.
  `pgh_schema` is in `list_display`: a row written under an older tag must not present a
  backfilled default as recorded data. They are registered automatically for every tracked model
  that is itself registered (`register_event_admins`, `apps/core/apps.py`), so the two lists
  cannot drift apart. Never add an admin action that writes to an event table.
- **All admin querysets go through `scope_to_tenant()`** — one function, which is what the leak
  tests can verify; a filter repeated in six overrides is not. A source-level test asserts every
  `get_queryset` calls it.
- **The aggregate events page** (`pghistory.admin`, `PGHISTORY_ADMIN_CLASS = TenantEventsAdmin`)
  has no tenant column of its own and cannot be given one: pghistory only lets a proxy field read
  `pgh_context`, and that table is deliberately free of identifiers. It does not need one —
  every event table here mirrors an `OwnedModel`, so each carries a real `owner_id` with the
  `tenant_isolation` policy, and the union is built from those tables: **without a tenant context
  the page returns nothing, not everything.** `.references(user)` adds the ORM-layer half.
  `PGHISTORY_ADMIN_ALL_EVENTS = False` — it unions *every* event table, so it stays blank until
  filtered. `PGHISTORY_ADMIN_ORDERING` cannot name `version`: the aggregate model has no such
  column (it shows up inside `pgh_diff`).
- `PGHISTORY_BASE_MODEL` names `apps.core.history.Event` rather than `@tracked` passing
  `base_model=`, so every event table — including one a third party or a migration generates —
  gets the same column set the union depends on.
- **`Lineage` admin is read-only and is not a `BaseModel` admin** (no version chain, no soft
  delete). Its generic pointers get no FK widget, so `source_link`/`target_link` resolve
  `(content_type, pgh_id)` to that event model's change page through one helper, which renders a
  placeholder rather than raising if a pointer ever fails to resolve — that is precisely when you
  want to be able to look.
- **Each owned object gets a Lineage page** (`admin_lineage.py`, button next to pghistory's
  "Events"): upstream, which exact source *versions* produced each version of it, and downstream,
  everything built from it with the version consumed — rows built from a superseded version are
  highlighted as stale. Tables, not a graph: they answer the questions people actually ask, and
  `lineage.graph()` is already there if a drawing is ever wanted.
- **Cross-tenant access for superusers** is a second database alias (`AUDIT_DB_ALIAS`, connecting
  as `app_admin`/BYPASSRLS — the role `shell_admin` uses), not a loosened policy: the default
  connection stays `app_user`, so `/api/ready`'s `rls` check keeps refusing a web process that
  could bypass the policies on its own connection. The alias exists **only when `DB_ADMIN_*` is
  configured**; unset — the production default — means a superuser sees their own tenant like
  anyone else. Every cross-tenant page view is logged to `tenant.admin_access`, and those pages
  are read-only (`owner` is `editable=False`, so such a page could not say whose a new row is).
  Tests for it need `transaction=True`: the alias is a second session and cannot see a normal
  test's uncommitted transaction, which is the same property that lets it see other tenants.
- Django's built-in "History" button still points at `LogEntry` and is unrelated to any of this.
  Leave it; the useful pages are "Events" and "Lineage".

## Data model conventions (`apps/core/models.py`, `apps/core/schemas.py`)

- **Primary keys are UUIDv7** (`BaseModel.id`, `db_default=Func(function="uuidv7")`, PG 18):
  time-ordered like an auto-increment id (index locality, sortable by creation), globally unique,
  and cheap to generate anywhere so offline-created rows never collide (NOTES.md §6). Native
  Postgres `uuid` column, and the default lives in the *database* so a raw INSERT or a data
  migration gets a well-formed id too (never set `default=` as well — Django would silently
  ignore `db_default`). We are deliberately locked
  in on Postgres. Ids are `NewType`s per model (`DatasetId = NewType("DatasetId", uuid.UUID)`),
  ninja path params are `uuid.UUID`, the generated TS types have `id: string`.
- **Every model extends `BaseModel`** (id, `created`, `modified`, `version`, `deleted_at`,
  `soft_delete()`, `set_payload()` for PUT, `set_payload_partial()` for PATCH — both pass values
  through as they are on the schema, so typed JSON fields keep their pydantic instances).
  `accounts.User` carries the same UUIDv7 id. The first four columns are set by the database
  (`db_default` / the `bump_version` trigger) and must never be assigned in Python; see
  "Versioning, history and lineage".
- **User data extends `OwnedModel`** (`owner` FK to `AUTH_USER_MODEL`, reverse accessor
  `user.<models>`) and is only read through `Model.objects.for_user(user)` (`OwnedQuerySet`,
  which also hides soft-deleted rows; `Model.all_objects` keeps the tenant scope and includes
  them) —
  the explicit half; `OwnedManager` adds the ambient tenant scope and Postgres RLS the
  guarantee (see "Multitenancy"). Services take the acting `User` first
  (`create_dataset(user, name=...)`, `get_dataset(user, id)`) and set `owner=user`; foreign
  rows raise the module's `*NotFound` → 404, never 403. `apps/core/tests/test_ownership.py`
  runs the HTTP isolation contract (empty list, 404 on get/put/patch/delete) for every resource
  in `RESOURCES` — register new owned modules there; `test_tenancy.py` covers the model layers
  automatically. Signed links (`/media/…?sig=`, downloads) are the one exception: the
  signature stands in for the user (download links carry the owner id and open their context).
- **Input schemas extend `StrictSchema`** (`extra="forbid"`): unknown fields are a 422 instead of
  being ignored (a typo in a PATCH would otherwise be a silent no-op). Output schemas stay
  `Schema`/`ModelSchema`. All of a module's schemas live in its `api.py`, next to the routes that
  use them.
- **Typed JSON columns**: `django_pydantic_field.SchemaField(Model, default=Model)`
  (`Dataset.options: DatasetOptions`) — stored as `jsonb`, validated on load and save, and a
  nested typed object in the API/TS types (NOTES.md §5). Give new fields defaults so old rows
  still load; put `extra="forbid"` on the pydantic model as well.
- **Lists are paginated** with ninja's `@paginate(PageNumberPagination)`: `?page=&page_size=`
  (`NINJA_PAGINATION_PER_PAGE=50`, max 500) → `{"items": [...], "count": n}` (`PagedDatasetOut`,
  `useListDatasets({ page })`). Services return the `QuerySet`; the view paginates it.
- Every module exposes the same surface: paginated `GET /x`, `POST /x` (201),
  `GET/PUT/PATCH/DELETE /x/{id}`. `apps/datasets` is the reference; `startmodule` reproduces it.

## Auth (`apps/accounts/`)

- Everything lives in `apps/accounts/api.py`: the schemas, the JWT handling, `BearerAuth`,
  `current_user()` and the routes (there is no `auth.py` and no `services.py`).
- Every API operation requires `Authorization: Bearer <token>` — `BearerAuth` (ninja
  `HttpBearer`) is installed globally in `config/api.py`. Public operations opt out with
  `auth=None` (health/ready, login, register, signed document downloads, `/api/docs`). Outside the
  API, `/media/<key>?sig=` is public-but-signed (see "Media files"). `TenantMiddleware`
  (`apps/core/middleware.py`) resolves the same header before the view and opens the tenant
  context; `BearerAuth` reuses its result (verified exactly once) — see "Multitenancy".
- Three token kinds, all resolved by `authenticate_bearer()`:
  1. JWT access tokens (HS256 with `SECRET_KEY`, claim `token_type=access`, stateless and
     therefore short-lived: `ACCESS_TOKEN_LIFETIME_MINUTES`, 15) from `POST /api/auth/login`
     (`{username, password}` → `{access_token, refresh_token}`). The refresh token is a second
     JWT (`token_type=refresh`, `REFRESH_TOKEN_LIFETIME_DAYS`, 30) whose `jti` is a
     `RefreshToken` row = the login session. `POST /api/auth/refresh` (`{refresh_token}`,
     public — the access token is expired by then) trades it for a new pair and revokes the
     old row (single-use); `POST /api/auth/logout` (`{refresh_token}`, public, always 204)
     revokes it. Expired/revoked/inactive user → 401 "Invalid or expired refresh token".
     Deliberately no reuse detection (ending every session when an old token shows up — two
     tabs refreshing at once would trigger it). Login purges the user's expired rows;
     `session_from_refresh_token()` returns `None` for anything spent. `GET /api/auth/me`
     returns the caller.
  2. Personal API tokens (`tk_…`, `ApiToken` model, never expire) for scripts/CI:
     `GET/POST /api/auth/api-tokens`, `DELETE /api/auth/api-tokens/{id}`.
  3. `API_FIXED_TOKEN` from the environment → acts as the first superuser (CI without DB state).
- `POST /api/auth/register` only works with `REGISTRATION_OPEN=true` (403 otherwise).
- Inside operations use `current_user(request)` (`apps/accounts/api.py`); helper functions take
  a `User`, never a request, and scope owned data with it (see "Data model conventions").
  Every other module imports it as `from apps.accounts.api import current_user`.
- Files that a browser fetches via plain `<a href>`/`<img src>` (no header) use signed, expiring
  URLs: `DocumentOut.download_url` carries `?sig=` (`documents/api.py::sign_download`),
  every `FileField.url` does too (`config/media.py::media_url`).
- Frontend: `src/lib/auth.ts` keeps the pair (localStorage `dx.access_token` /
  `dx.refresh_token`, `useAccessToken()`; the `storage` event keeps open tabs in sync).
  `custom-fetch.ts` adds the header; on a 401 it calls the generated `refreshToken()` once
  (single-flight — requests failing together share it, the refresh token being single-use),
  stores the new pair and retries the request. Only when the refresh is rejected too are the
  tokens dropped and `routes/__root.tsx` redirects to `/login` (`routes/login.tsx`,
  `useLogin()`); a rejection because another tab already rotated the pair falls back to that
  tab's tokens (`reloadTokens()`). Network errors keep the session (offline: the refresh
  happens at the next 401 after reconnecting). Logout (`__root.tsx`) posts the refresh token
  to `logout()`, then clears tokens + query cache.
- Dev: `manage.py createadmin` (admin/admin), `manage.py token [-m MINUTES]` prints an access
  JWT for curl; anything unattended should use a personal API token instead.
- The user model is our own: `apps.accounts.models.User` (`AUTH_USER_MODEL = "accounts.User"`,
  `AbstractUser` + UUIDv7 id). Import it from there (or `get_user_model()`), never from
  `django.contrib.auth.models`; extend it in place instead of adding a profile table.

## Media files (stored in S3, served by Django)

Rule: **bytes live in the object store, every URL a client sees is a Django URL.** No presigned
S3 links, no public bucket, no CDN in front of the store — one origin (WhiteNoise-style), the
store stays private, and the bundled compose works without a browser-reachable `s3` host.

- Storage: Django's default storage (`STORAGES["default"]`) is `config.media.S3MediaStorage`
  (django-storages `S3Storage` + boto3) whenever `MEDIA_STORAGE=s3` (the default);
  `MEDIA_STORAGE=local` uses `config.media.LocalMediaStorage` (`FileSystemStorage` under
  `backend/media`). Tests always use local (`settings_test.py`), with `MEDIA_ROOT` pointed at a
  tmp dir per test. Models just declare `FileField`/`ImageField` — never pick a storage per field.
- Serving (`config/media.py`): `MEDIA_URL = /media/` is a Django route,
  `path("media/<path:path>", serve_media)` in `config/urls.py` (before the SPA catch-all, which
  excludes `media/`). `serve_media` verifies the signature, opens the key on the default storage
  and streams it as a `FileResponse` (inline, content type from the file name,
  `Cache-Control: private, max-age=MEDIA_LINK_MAX_AGE`); 403 JSON without a valid link, 404 JSON
  for a missing object.
- Links: both storage classes share `SignedMediaUrlMixin`, so **`field.url` is the only way to
  link to a file** and yields `/media/<key>?sig=…` — signed with `SECRET_KEY`
  (`django.core.signing`, salt `media.url`), valid for `MEDIA_LINK_MAX_AGE` (1 h). Browsers
  fetch them with plain `<img src>` / `<a href>` (no bearer header), which is why the route is
  public-but-signed instead of authenticated. In the SPA wrap them in `apiUrl()`
  (`src/lib/custom-fetch.ts`) — a no-op on the web, absolute for the Capacitor build. Put `file.url` into API schemas when a client
  needs a file (`resolve_*` on the ninja schema); never hand out storage keys or bucket names.
- Documents keep their own `GET /api/documents/{id}/download?sig=` (`DocumentOut.download_url`):
  same signing idea, but forces a download with the original file name; the payload is
  `[document id, owner id]` so the view can open that owner's tenant context (links signed
  before this change stop working — they expire within the hour anyway). Use `/media/…` for
  inline display and any other model's files.
- Scale note: Django streams every byte (gunicorn workers are busy for the duration). Fine for
  this app's file sizes; if it ever hurts, the seam is `SignedMediaUrlMixin.url()` — switch it
  to presigned store URLs (needs a public endpoint setting) without touching models or clients.
- Access control is per link, not per user: anyone holding an unexpired link can fetch the file.
  If files ever become user-scoped, sign the user id in as well and check it in `serve_media`.
- Tests: `apps/core/tests/test_media.py` (signed link works anonymously, unsigned/foreign/expired
  → 403, missing → 404, SPA catch-all does not swallow `/media/`).

### Object store (S3-compatible)

- Dev store: the compose `s3` service — **RustFS** (`rustfs/rustfs`, Apache-2.0 MinIO drop-in;
  MinIO stopped publishing community images in 2025). S3 API on host port **9100**, web console on
  **9101**, credentials `dx` / `dxdxdxdx`, data in the `s3data` volume. Ports 9000/9001 are
  deliberately not used (clash with Hadoop etc. on dev machines). Env: `S3_ENDPOINT_URL`
  (`http://localhost:9100`; empty = real AWS), `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`
  (`dx-media`), `S3_REGION`. Path-style addressing + SigV4 are set in `settings.py`; keep them
  for any MinIO-like server.
- `manage.py ensure_bucket` (`apps/core/management/commands/`, logic in `apps/core/storage.py`)
  creates the media bucket (`S3_BUCKET`) and the backup bucket (`S3_BACKUP_BUCKET`) if missing
  and enables **bucket versioning**; idempotent. `./scripts/db.sh`
  runs it after the containers are healthy and `docker/entrypoint.sh` before `migrate`, so a
  fresh checkout/container is ready without manual steps. Do not create buckets anywhere else.
- Keys are `upload_to` + the upload name (`documents/<owner id>/%Y/%m/<name>`,
  `apps.core.models.owned_upload_path` — one prefix per tenant); `file_overwrite=False`
  makes django-storages append a random suffix on a clash, so files never share or overwrite a
  key. Deduplication is
  deliberately **not** done in the app (decision 2026-08-28): no S3-compatible server dedups by
  content, so if it is ever wanted it belongs below the store (e.g. ZFS/btrfs dedup on the data
  volume), not in Django.
- Versioning: deleting a document writes a delete marker, the previous version stays (recoverable
  in the console / `list_object_versions`); nothing in the app restores versions yet, and there
  is no lifecycle rule expiring old versions — add one before the store gets big.
- Integration test against the real store: `uv run pytest -m slow apps/documents/tests/test_s3.py`
  (skips when the store is down; uses a throwaway bucket). Everything else stays hermetic.
- Backups: database dumps go to the `dx-backups` bucket (see "Backups"); the uploaded objects
  themselves live in the `s3data` volume / media bucket — back up the store, not the app.

## Background tasks (Celery)

- `config/celery.py` builds the app (`celery -A config`), reads every `CELERY_*` setting from
  `config/settings.py`, autodiscovers `tasks.py` in all apps. `config/__init__.py` imports it so
  `@shared_task` binds to it. Broker + result store: Redis (Valkey in `docker/docker-compose.yml`,
  started by `./scripts/db.sh`); `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` (defaults to the broker).
- Dev runs a real worker, like production: `./scripts/celery.sh` (= `manage.py celery_dev`,
  logic in `apps/core/worker_reload.py`) runs `celery worker --concurrency=1` and restarts it
  when a `.py` file under `apps/` or `config/` changes (watchfiles; Celery has had no reloader
  since 4.0). Restarts are warm: the worker gets SIGTERM, running tasks finish (`--stop-timeout`,
  default 30 s, then SIGKILL), reserved ones return to the queue. SIGTERM on purpose — Celery
  swallows a SIGINT that arrives during its first second of startup (measured), so
  `watchfiles`' CLI (SIGINT only) would hang for the timeout on every rapid double save. The
  worker runs in its own session; Ctrl+C or `kill` on the reloader stops both. Without a worker,
  enqueued tasks simply wait in Valkey.
- **Eager mode** (`CELERY_EAGER=true`, opt-in): tasks run inline in the caller, no worker needed
  — but no progress events either. Failures are stored on the result (`state=FAILURE` +
  exception) instead of raising into the request (`CELERY_TASK_EAGER_PROPAGATES=False`), so the
  API behaves the same as with a worker. Tests always run eager with an in-memory result store
  (`config/settings_test.py`).
- Pattern (`apps/core/tasks.py`): the task body is the work — **`@tenant_task` for anything
  that touches owned data**
  (first argument `owner_id`, see "Multitenancy"); an endpoint enqueues (`task.delay(...)`) and returns
  `TaskOut` (`id`, `state`, `ready`, `result`, `error`, `progress`, `stream_url`) built by
  `tasks.status_of()`. Long tasks report progress with
  `self.update_state(state=PROGRESS, meta={"current": i, "total": n})` (skip when
  `self.request.is_eager`). Flaky work: `base=WithRetry` (`config/celery.py`, backoff + jitter).
- Following a task: `GET /api/tasks/{id}/events` streams Server-Sent Events (`event: status`,
  data = a `TaskOut` JSON) — one now, one per state change, then the connection closes when the
  task is `ready` (also after `tasks.WATCH_TIMEOUT`, EventSource reconnects by itself). The
  server polls the result store (`tasks.watch()`, `WATCH_INTERVAL`) and repeats the unchanged
  status every `WATCH_HEARTBEAT` seconds. `EventSource` cannot send headers, so the endpoint is
  public but signed (`TaskOut.stream_url`, `tasks.sign_stream()`, valid 24 h, listed in
  `PUBLIC_OPERATIONS`); it is a streaming response outside the JSON contract like document
  downloads. `GET /api/tasks/{id}` (plain JSON) remains for polling/one-off lookups. Frontend:
  `routes/tasks.tsx::useTaskStream` writes each event into the TanStack cache
  (`getGetTaskQueryKey`), so `useGetTask` renders it; polling only kicks in if the stream is
  closed for good. The bundled image runs gunicorn with `gthread` workers because every open
  stream occupies a thread.
- Durability (`settings.py`): `task_acks_late` + `task_reject_on_worker_lost` +
  `worker_prefetch_multiplier=1` — a task leaves the queue only after it finished, so a worker
  crash means it runs again (tasks must be idempotent). Redis/Valkey has no real acks: after a
  hard kill (SIGKILL) the task is redelivered only after `visibility_timeout` (2 h, must exceed
  the longest task); a normal `Ctrl+C`/SIGTERM finishes running tasks first. The dev Valkey
  persists its queue + results to the `valkeydata` volume (AOF, fsync every second), so nothing
  is lost across container restarts.
- Task modules need `from __future__ import annotations`: Celery inspects signatures at runtime
  and celery-types' `Task[P, R]` is not subscriptable at runtime.
- Samples wired to the frontend (`/tasks`, `routes/tasks.tsx`, hooks in `src/api/tasks/`):
  `POST /api/tasks/add|count|dataset-summary|fail`, `GET /api/tasks/{id}`.
- Periodic tasks: `CELERY_BEAT_SCHEDULE` in `settings.py` (file-based on purpose — the schedule
  is code, reviewed and deployed like code; times in UTC). Currently the nightly
  `backup_database`, routed to the `maintenance` queue (`CELERY_TASK_ROUTES`): it dumps every
  tenant, so only the maintenance worker — beat embedded, connected as the table owner —
  consumes that queue: `./scripts/celery.sh maintenance` (the prod stack's `beat` service).
  The regular workers run as `app_user` and never see it.
- Bundled image: compose `--profile app` also starts `worker` (same image, `celery … worker`).

## Logging (`config/logging.py`)

- One pipeline, two renderings: `logging.getLogger()` (Django, Celery, libraries) and
  `structlog.get_logger()` (our code) share one `ProcessorFormatter`. **`LOG_FORMAT=console`**
  (default with `DEBUG` — what `./scripts/serve.sh` shows) prints plain developer output,
  `HH:MM:SS LEVEL message key=value …` (time in `TIME_ZONE`, like runserver's own lines): one
  line per request (`GET /api/health 200`, 4xx as
  `WARN`, 5xx as `ERROR`), plain Python tracebacks, Celery's own task lines, the module name
  appended for our code and for warnings/errors. **`LOG_FORMAT=json`** (default without
  `DEBUG`, i.e. the docker image) prints one JSON object per line with the full structlog
  context for Loki/CloudWatch & co. `LOG_LEVEL` is the root level; `LOG_SQL=true` (dev only)
  logs every query.
- The console format deliberately hides what only matters for correlating lines in a log
  store: `request_id`, `user_id`, `ip`, task ids, `request_started`, and django-structlog's
  `task_started`/`task_succeeded` events (the worker's "received"/"succeeded" lines say the
  same). `compact_dev_events` + `DevRenderer` in `config/logging.py`; JSON keeps everything.
- Log events, not sentences: `log = structlog.get_logger(__name__)`;
  `log.info("dataset_imported", dataset_id=str(dataset.pk), rows=n)`. The event name is a
  constant, the key/value pairs are what you filter on later — no f-strings.
- django-structlog (`RequestMiddleware`, last in `MIDDLEWARE`) binds `request_id`, `user_id`
  (re-bound after the view, so the bearer-authenticated API user shows up) and `ip` to every
  line of a request and logs `request_started`/`request_finished` (status, path);
  `django.server`'s request lines and Django's "Not Found: /x" per 4xx are silenced to avoid
  duplicates (`django.request` stays at `ERROR`: its "Internal Server Error: /x" carries the
  traceback), Django's DEFAULT_LOGGING handlers are removed (else every Django line prints
  twice), and stdlib `extra=` is not carried into lines (Django/Celery put objects there). The Celery boot step in `config/celery.py`
  (`DJANGO_STRUCTLOG_CELERY_ENABLED`) carries the ids into task logs, and its `setup_logging`
  receiver (`configure_worker_logging`) makes the worker use `LOGGING` instead of Celery's own
  root handler — so the worker prints the same plain/JSON lines as the web process.
- Tests: `apps/core/tests/test_logging.py` (both formats; stdlib records get the context; the
  console compaction; Django's handlers gone; the worker hook).

## Backups (`apps/core/backups.py`)

- A dump holds every user's rows: `backup`/`restore` need a connection RLS does not apply to
  (`./scripts/backup.sh`/`restore.sh` set `DB_ROLE=migrator`; the nightly task runs on the
  maintenance worker) and refuse the runtime role (`CrossTenantAccessRequired`) rather than
  silently writing a partial dump. `restore` also re-applies the RLS policies after `migrate`.
- `manage.py backup` (`./scripts/backup.sh`) dumps the database with `dumpdata` (natural keys;
  no content types/permissions/sessions/log entries) into `dx-<UTC timestamp>.json.gz` in the
  **`backups` storage** (`STORAGES["backups"]`): the `S3_BACKUP_BUCKET` bucket (`dx-backups`,
  versioned, created by `ensure_bucket`), or `backend/backups/` with `MEDIA_STORAGE=local`.
  `--list` shows the dumps, `--prune` keeps only the newest `BACKUP_KEEP` (30).
- `manage.py restore <name>|--latest [-y]` (`./scripts/restore.sh`) = `migrate` + `loaddata`:
  rows with the same pk are overwritten, nothing is deleted. A dump contains the event tables as
  well (`use_base_manager=True`, so soft-deleted rows are in it too), and the load runs inside
  `history.unversioned()` — otherwise the triggers would bump every restored version, duplicate
  its history, and then fail outright on an event row that already exists. `./scripts/roundtrip.sh` proves it
  against a fresh database (dev only, forces `MEDIA_STORAGE=local`).
- Nightly: `apps.core.tasks.backup_database` (`CELERY_BEAT_SCHEDULE`, 03:00 UTC, `WithRetry`)
  creates a dump and prunes; needs a running `beat`.
- Not in a dump: uploaded files (they live in the versioned media bucket — back up the store)
  and the Celery queue. For a production Postgres the provider's `pg_dump`/snapshots remain the
  primary backup; this is the app-level, restore-anywhere copy.
- Tests write to a throwaway directory (`settings_test.py`), never to a bucket:
  `apps/core/tests/test_backups.py`, command tests in `test_commands.py`.

## Health checks (`apps/core/health.py`)

- `GET /api/health` — liveness: the process answers requests; touches nothing (a database outage
  must not get the container restarted). Body `{"status": "ok"}`.
- `GET /api/ready` — readiness: `database` (`SELECT 1`), `migrations` (nothing unapplied),
  `rls` (every owned table has its policy, and the connection's role is subject to it — a
  process connected as owner/superuser/`BYPASSRLS` is not ready), `celery` (broker reachable;
  "eager mode" when tasks run inline), `storage:default` and
  `storage:backups` (buckets exist; "local disk" otherwise). Body
  `{"status": "ok"|"fail", "checks": [{name, ok, detail}]}`, HTTP **503** when any check fails —
  compose/Docker health checks and load balancers gate on this one. Both are public
  (`PUBLIC_OPERATIONS`). The home page (`routes/index.tsx`) shows both; `useReady()` reads the
  503 body from the `ApiError`.

## Management commands (django-click + rich)

- Commands are click commands (`import djclick as click`, the function is named `command`,
  `@click.command()` + `@click.argument`/`@click.option`), not `BaseCommand` subclasses: typed,
  validated options, `--help`, prompts and confirmations for free; Django's `--settings`,
  `--pythonpath`, `--traceback`, `-v` still work. Output through rich (`Console`, `Table`,
  `Panel`, progress bars); `click.echo(..., nl=False)` only for bare values scripts capture
  (`token`). User-facing failures: `raise click.ClickException("…")` (exit 1, red); wrong usage:
  `click.UsageError` (exit 2).
- **`manage.py hello_world [NAME] [--shout] [-n N]`** is the reference implementation (argument,
  options, rich panel + table, structured log line) — copy it for new commands. Logic beyond
  parsing and printing goes to a service module; long work is a Celery task.
- Existing: `createadmin`, `token` (accounts); `ensure_bucket`, `backup`, `restore`,
  `startmodule`, `hello_world`, `rls_sync`, `pull_tenant`, `load_tenant` (core). Exception to
  the click rule: `shell_as` and `shell_admin` subclass Django's `shell` command (`BaseCommand`)
  so the REPL runs in-process inside the pinned tenant context; test them with `call_command`.
- Tests use `click.testing.CliRunner().invoke(module.command, [...])` and assert on
  `result.exit_code` / `result.output` (`call_command` does not understand click options).
- django-click ships no type hints: the mini-stub `backend/stubs/djclick/__init__.pyi`
  (`mypy_path = "stubs"`) re-exports click's types. Add stubs there for other untyped packages
  instead of `ignore_missing_imports`.

## Type checking (`scripts/check.py`)

`./scripts/check.py [backend|frontend]` runs mypy and `tsc -b`, every step regardless of earlier
failures, and exits 1 if any fails (`./scripts/ci.py` = the same steps + `./scripts/build.sh`,
the production image). Stdlib-only Python (no venv needed for the script; `uv run`
supplies the backend's), sources `scripts/_pnpm.sh` for pnpm. Policy: **strict, plus every
check that catches a logic error — none that polices style.** Null/undefined handling first.
The configuration lives with the tools, not in the script:

- **Backend** (`backend/pyproject.toml [tool.mypy]`): `strict` + django-stubs plugin, plus
  `warn_unreachable` (dead branches, `is None` on a non-optional), `disallow_any_unimported`
  (an untyped import must not silently become `Any` — write a stub under `backend/stubs/`
  instead, as for `djclick` and `storages`) and the error codes `possibly-undefined`,
  `redundant-expr`, `truthy-bool`, `truthy-iterable`, `exhaustive-match`, `deprecated`,
  `unused-awaitable`, `ignore-without-code`. In practice: `# type: ignore[code]` only; `if obj:`
  on a non-optional type is an error (write `is not None`, or the type is missing `| None`);
  the test client's response cannot be `isinstance`-narrowed to `StreamingHttpResponse`
  (`cast` it, see `test_tasks.py`). Signatures are forced by ruff `ANN` (every parameter and
  return annotated, no `Any` in them). There is no `disallow_any_explicit` override any more:
  the logic now sits in `api.py` beside ninja's `Field(...)`, which is typed `Any`. Keep `Any`
  out of your own signatures anyway (`UploadedFile[bytes]`, not `[Any]`).
- **Frontend** (`frontend/tsconfig.app.json` + `tsconfig.node.json`, checked by `tsc -b` — the
  same call `pnpm build` makes): `strict` (the TS 6 default, kept explicit),
  `noUncheckedIndexedAccess` (`xs[0]` and `record[key]` are `T | undefined` — handle it),
  `noImplicitReturns`, `noImplicitOverride`, `noUncheckedSideEffectImports`,
  `allowUnreachableCode: false`, `allowUnusedLabels: false`. Explicit signatures are forced
  by Biome (`biome.json`): the recommended set bans `any`, `!`, untyped `let` and evolving
  types, and `nursery/useExplicitType` requires a return type on every function and a type on
  every parameter that is not inferred from a call argument — components return
  `JSX.Element` (`import type { JSX } from "react"`), handlers `void`, JSX-attribute arrows
  are written `(event: ChangeEvent<HTMLInputElement>): void => …`, object-property callbacks
  (`onSuccess`, `refetchInterval`, `beforeLoad`) type their parameter explicitly, module-level
  consts that are not literals get a type. Callbacks passed straight to a call (`.map(fn)`,
  `setState(fn)`) are exempt. `src/components/ui/**` (vendored shadcn) is excluded so
  `shadcn add` stays frictionless.
- **Deliberately off**: `exactOptionalPropertyTypes` (fights library and orval-generated types —
  `options?: T` vs a form's `T | undefined`), `noPropertyAccessFromIndexSignature` (style, not
  safety), mypy `disallow_any_explicit` (Django needs `Any` at its edges). Formatting and line
  length are ruff/Biome's business (`./scripts/lint.sh`), never the type checkers'.

## Testing

Strategy: **fast, hermetic, layered; every route private by default; contracts enforced by
tests, not by review.** Backend: pytest + pytest-django against the dev Postgres (`test_dx`
database, created per run), settings `config/settings_test.py` (= real settings + eager Celery
with in-memory results + MD5 password hasher). Run `./scripts/test.sh`, coverage with
`./scripts/coverage.sh`; the whole suite takes well under a second, keep it that way.

Layers (each app mirrors this in `tests/`):

1. **Unit tests** (inside `test_api.py`, or a topic file such as `test_tags.py`): call the
   module functions in `api.py` directly — domain rules, validation, the `HttpError` a lookup
   raises. Most cases live here; no HTTP, no auth. Wrap them in `with acting_as(user):`.
2. **API tests** (`test_api.py`, auto-marked `api`): the HTTP contract through Django's test
   client — status codes, JSON shape, error `detail`, one happy path per endpoint. Use
   `auth_client`; `client_for(other_user)` for ownership/isolation ("B gets 404 for A's
   things"); the anonymous `client` only for public endpoints and 401 checks.
3. **Cross-cutting guarantees** (`apps/core/tests/`): `test_security.py` — every operation is
   authenticated unless listed in `PUBLIC_OPERATIONS` with a reason, plus an automatic anonymous
   request against every path in the spec (must be 401), docs need a staff session;
   `test_openapi.py` — operation ids unique/snake_case and `openschema.json` in sync with the
   code; `test_errors.py` — JSON error bodies; `test_ownership.py` — other users get an empty
   list and 404s for every owned resource in `RESOURCES`; `test_tenancy.py` — ORM scope + RLS
   isolation, fail-closed, `WITH CHECK`, middleware, tasks, scrubbers and source rules for every
   owned model; `test_history.py` — every `BaseModel` is tracked or exempt, triggers survive
   `Meta` inheritance, bulk and raw writes are versioned, event tables are append-only, soft
   delete is a version, unique constraints are conditional, the schema log is current, plus the
   lineage and revision-page behaviour end to end;
   `test_models.py` — UUIDv7 keys, `for_user`, payload helpers; `test_schemas.py`
   — `StrictSchema`.
4. **Infra** (`test_commands.py`, auto-marked `infra`): management commands (invoked with
   `click.testing.CliRunner`, see "Management commands") and dev tooling. `test_deploy.py`
   (marked `infra` explicitly) runs `check --deploy` in a subprocess with a production
   environment — the settings must pass Django's checklist or the image refuses to start.
   `test_env.py` covers the `config/env.py` translations (`DATABASE_URL`, `EMAIL_URL`) and guards.
5. **Frontend** (planned per NOTES.md §2, not set up yet): Vitest + Testing Library for
   components/feature code (mock the generated hooks, never `fetch`), Playwright for a few e2e
   flows (login, one CRUD, one task) against the bundled image.

Conventions:

- Fixtures live in `backend/conftest.py`: `user`, `other_user`, `staff_user`, `client_for`,
  `auth_client`. Create data through the module's own functions
  (`create_dataset_for(user, ...)` from `apps/datasets/api.py`), not raw ORM calls,
  and inside `with acting_as(user):` (`apps.core.testing`) — writes need the tenant context
  (see "Multitenancy"); requests get it from the middleware. Every database test runs as the
  runtime role (`SET ROLE app_user`, RLS enforced); the suite itself connects as the migrator.
- Markers are registered in `pyproject.toml` (`--strict-markers`): `api`, `infra`, `slow`,
  `cross_tenant` (keep the owner's role — backup/restore tooling only).
  `pytest -m "not api"` = fast unit run; `slow` must be set by hand.
- Warnings fail the suite (`filterwarnings = error`); allow-list third-party noise explicitly.
- Tests are named for the behaviour (`test_login_rejects_wrong_password`), one behaviour each;
  assert on full JSON bodies where the shape is the contract.
- New endpoint checklist: service test → API test (happy path + one error) → run
  `test_security.py` (add to `PUBLIC_OPERATIONS` only with a reason) → `./scripts/sync_schema.sh`.
- `./scripts/check.sh` = what CI runs (ruff, mypy, pytest, Biome, tsc + vite build, spec drift).

## Serving the SPA from Django (WhiteNoise)

- Vite builds with `base: "/static/"`; `FRONTEND_DIST` (default `../frontend/dist`) is in
  `STATICFILES_DIRS`, so `collectstatic` copies `index.html` + `assets/` into
  `backend/staticfiles/`, and WhiteNoise serves them under `/static/` (gzip + brotli).
- `config/urls.py` ends with a catch-all that serves `staticfiles/index.html` for every path not
  starting with `api/`, `admin/`, `static/`, `media/` — deep links work, HTML is
  `Cache-Control: no-cache`.
  Without a build it returns 503 with a hint.
- Hashed Vite assets (`name-<8 chars>.ext`) get immutable cache headers
  (`config/static.py::vite_immutable_file`); storage is `CompressedStaticFilesStorage` (no
  Django manifest hashing — Vite already hashes).
- In dev nobody uses this: Vite on :5173 serves the app and proxies `/api`. WhiteNoise runs in
  finder/autorefresh mode when `DEBUG=true`.

## Production (`docker/`)

Settings follow Django's deployment checklist: `manage.py check --deploy --fail-level WARNING`
passes with a production environment (enforced by `test_commands.py`), and `docker/entrypoint.sh`
runs it before serving, so a misconfigured container refuses to start. Configuration is
environment-only (`config/env.py`; `docker/.env.prod.example` lists a complete production set).

- **Database roles**: `docker/postgres/10-roles.sh` runs on a fresh `db` volume (passwords
  `DB_APP_PASSWORD`, `DB_MIGRATOR_PASSWORD` in `.env.prod`); for an older volume run it once
  by hand (comment in the compose file). `app`/`worker` connect as `app_user` (`DATABASE_URL`)
  and carry `DB_MIGRATOR_*` for the entrypoint's `migrate` → `rls_sync` → `rls_sync --check`;
  `beat` is the maintenance worker (`DB_ROLE=migrator`). `app_admin` gets no password from
  `.env.prod` (it is every service's `env_file`): set one ad hoc
  (`POSTGRES_ADMIN_PASSWORD=… ./scripts/prod.sh up -d db`) and use it from an ops shell only.
  Stricter setups: `MIGRATE_ON_START=false`, run the three steps as a release job and drop
  `DB_MIGRATOR_*` from the web service.
- **Secrets and hosts**: `SECRET_KEY` (the dev default is refused when `DEBUG=false`;
  `SECRET_KEY_FALLBACKS` for rotation), `ALLOWED_HOSTS` (JSON list; loopback names are always
  appended for the container health check), `EMAIL_URL` (`smtp://user:pw@host:587?tls=true`,
  `smtps://…`, or an explicit `dummy://` — the console backend fails the deploy check;
  `DEFAULT_FROM_EMAIL`).
- **HTTPS** (`HTTPS_ONLY`, default `not DEBUG`): redirect to https (the probes are exempt), secure
  session/CSRF cookies, HSTS (`SECURE_HSTS_SECONDS`, default 1 h — raise to a year once stable;
  `includeSubDomains` + `preload` are on) and `SECURE_PROXY_SSL_HEADER` = `X-Forwarded-Proto`.
  The app never terminates TLS: port 8000 must only be reachable through the proxy, which
  overwrites that header. `HTTPS_ONLY=false` is for plain-http smoke tests only and silences
  exactly the related checks (`SILENCED_SYSTEM_CHECKS`).
- **Cache**: Valkey/Redis (`CACHE_URL`, db 1; Celery uses db 0) through Django's `RedisCache`
  (`KEY_PREFIX=dx`, 2 s socket timeouts), shared by every gunicorn and Celery process; sessions
  use `cached_db` (cache in front, database behind). Tests use `LocMemCache`
  (`settings_test.py`). `DB_CONN_MAX_AGE` (60 s) keeps database connections open with
  `CONN_HEALTH_CHECKS`; `DATABASE_URL` query parameters (`?sslmode=require`) become psycopg
  options.
- **Errors**: `SENTRY_DSN` enables Sentry (Django/Celery/Redis integrations, `send_default_pii`
  off, release = `APP_VERSION`); logs go to stdout as JSON (`config/logging.py`).
- **Image** (`docker/Dockerfile`; `./scripts/build.sh` tags `dx-app:latest` and passes the git
  commit as build arg `APP_VERSION`): `node:24-alpine` runs `pnpm build`; `uv sync --frozen
  --no-dev` in a builder stage; the runtime stage copies the venv + `dist/`, runs `collectstatic`
  (placeholder `SECRET_KEY`), byte-compiles and runs as the unprivileged `app` user. gunicorn:
  `docker/gunicorn.conf.py` (`gthread`, `WEB_CONCURRENCY` × `GUNICORN_THREADS`, preload, worker
  recycling; SSE streams hold a thread each). `HEALTHCHECK` = `/api/ready` via loopback.
  Entrypoint: deploy check → wait for the DB → (`gunicorn` command only, unless
  `MIGRATE_ON_START=false`) `ensure_bucket` + `DB_ROLE=migrator migrate` + `rls_sync` +
  `rls_sync --check` → exec. Worker and beat run the same image with a different command and
  start once `app` is healthy.
- **Stack** (`docker/docker-compose.prod.yml`, configured by `docker/.env.prod` from
  `docker/.env.prod.example`, run with `./scripts/prod.sh`): `caddy` (ports 80/443, automatic
  Let's Encrypt certificates for `DOMAIN`, `docker/Caddyfile`) → `app`; `worker` and `beat`
  (`CELERY_BEAT_SCHEDULE`, nightly backup); `db`, `redis` (AOF), `s3` (no console) with named
  volumes and no published ports; json-file log rotation. Any key from `config/env.py` can go
  into `.env.prod`; for managed services set `DATABASE_URL`/`CACHE_URL`/`CELERY_BROKER_URL`/`S3_*`
  there and drop the matching service. Plain-http smoke test of the image: `./scripts/build.sh
  --run` (dev compose `app` profile on :8080 with `HTTPS_ONLY=false`, `EMAIL_URL=dummy://`).
- Still open: host-level backup of the `s3data` volume (the nightly dump lands in the same
  store), `clearsessions` for expired admin sessions, a lifecycle rule for old object versions,
  and resource limits.

## Dev database (`docker/docker-compose.yml`)

- `postgres:18-alpine`, superuser/password/db = `dx`/`dx`/`dx` (the dev migrator), port 5432,
  named volume `pgdata` mounted at `/var/lib/postgresql` (Postgres 18 image layout). No
  pgbouncer in dev. `docker/postgres/10-roles.sh` (mounted into `docker-entrypoint-initdb.d`,
  re-run by `./scripts/db.sh`) adds `app_user`/`app_migrator`/`app_admin` (passwords = names)
  and hands existing tables to `app_migrator`; the app connects as `app_user` (see
  "Multitenancy").
- Also `redis` (Valkey, :6379, append-only persistence in volume `valkeydata` — see
  "Background tasks") and `s3` (RustFS, :9100/:9101, volume `s3data` — see "File storage").
  `./scripts/db.sh` waits until every healthcheck passes, then runs `ensure_bucket`.
  Reset everything (DB + queue + uploads): `./scripts/db.sh down -v`.

## Frontend (`frontend/`)

- Vite 8 (rolldown), React 19, TypeScript 6, Tailwind 4 (`@tailwindcss/vite`), shadcn/ui,
  TanStack Router (file-based, `autoCodeSplitting`), TanStack Query, Zod 4, orval, Biome (lint +
  format, single tool — no ESLint/oxlint). Package manager is pnpm; lockfile `pnpm-lock.yaml`.
  pnpm 11 blocks dependency build scripts by default; approvals live in
  `frontend/pnpm-workspace.yaml` (`allowBuilds`), added with `pnpm approve-builds <pkg>`.
- Layout:
  - `src/main.tsx` — `QueryClientProvider` + `RouterProvider` (`defaultPreload: "intent"`).
  - `src/routes/` — one file per route: `__root.tsx` (layout + nav `navItems`), `index.tsx` (`/`),
    `datasets.tsx` (`/datasets`), `documents.tsx` (`/documents`, multi-file drag & drop upload),
    `login.tsx` (`/login`, the only page without a token — see "Auth"), `tasks.tsx` (`/tasks`,
    triggers the sample Celery tasks and polls their status), `gallery.tsx` (`/gallery`, image
    and video uploads rendered inline from their signed `url`),
    `history.$resource.$objectId.tsx` (`/history/dataset/<id>`, the revision page — every version
    of one object grouped by save, with diffs and lineage links; linked from every list page).
    `reports.tsx` → `/reports`, `reports/$id.tsx` → `/reports/:id`.
  - `src/routeTree.gen.ts` — generated by the router plugin on `pnpm dev`/`pnpm build`;
    committed, excluded from Biome. Never edit. If missing, run `pnpm exec vite build` once
    (`pnpm build` runs `tsc -b` first and would fail without it).
  - `src/api/` — generated by orval (see "End-to-end types"); the only API access path.
  - `src/features/<feature>/` — feature code and on-demand modules such as
    `datasets/export-csv.ts`. No API types or fetchers here.
  - `src/lib/custom-fetch.ts` — transport for the generated client (bearer header, 401 handling);
    `src/lib/auth.ts` — access-token store; `src/lib/utils.ts` — `cn()`;
    `src/lib/format.ts` — `formatBytes()`.
  - `src/components/ui/` — shadcn components (button, card, input, label, table). Repo-owned.
    `src/components/upload-form.tsx` — shared multi-file drag & drop picker (documents, gallery);
    the page owns the upload mutation and passes `onUpload`/`pending`/`error`.
  - `src/vite-env.d.ts` — typed `import.meta.env` (`VITE_API_URL`).
- Adding a page: create `src/routes/<path>.tsx` exporting
  `export const Route = createFileRoute("/<path>")({ component: Page })`, add a nav entry in
  `__root.tsx` if needed. The route automatically becomes its own chunk, loaded on first visit
  (preloaded on link hover).
- Heavy or rarely used code is loaded inside the handler that needs it:
  `const { exportDatasetsCsv } = await import("@/features/datasets/export-csv")`. Use this for
  Excel/PDF/chart/editor libraries — never import them at module top level in shared code.
- Chunking (`vite.config.ts`): `build.rolldownOptions.output.codeSplitting.groups` puts
  react/react-dom/scheduler/@tanstack into a stable `vendor` chunk (Vite 8 API; `manualChunks`/
  `advancedChunks` are deprecated). `rollup-plugin-visualizer` writes `frontend/stats.html`
  (git-ignored) on every build — check it before optimizing.
- `base` is `/static/` for builds (Django/WhiteNoise serve assets there) and `/` in dev.
- Import alias `@` → `src/` (in `tsconfig*.json` and `vite.config.ts`; no `baseUrl` — TS 6
  deprecates it). `import.meta.dirname` instead of `__dirname` in config files.
- shadcn: `components.json` uses the `radix-nova` style (neutral base, CSS variables, Lucide icons,
  Geist font). Add components with `pnpm dlx shadcn@latest add <name>`.
- Dev server: port 5173, proxies `/api` and `/media` → `http://localhost:8000` without path
  rewrite, so the app always uses relative `/api/...` and `/media/...` (same-origin in prod, see
  "Serving the SPA"). Anything else the browser fetches from Django needs a proxy entry here. To test against a
  second Django (e.g. `PORT=8001 ./scripts/serve.sh`) without
  touching the running one: `API_PROXY_TARGET=http://127.0.0.1:8001 pnpm exec vite --port 5174`.
- Biome: 2-space indent, double quotes, recommended + React rules, import sorting. Auto-fix with
  `pnpm biome check --write .`. Avoid non-null assertions (`!`); Biome flags them. Linting is
  disabled for `src/api/**` (generated), formatting/import order still apply.

## Bundle discipline (see `NOTES.md` §7)

- The app will grow to tens of MB of source; nobody ever downloads "the app", only the shell plus
  the visited route. Current initial load for `/`: entry ~37 KB + vendor ~300 KB (95 KB gzip) +
  CSS; home route 3 KB; datasets route ~73 KB (20 KB gzip, includes Zod — it lands in a shared
  chunk once more routes use it); `export-csv` 0.5 KB, loaded on click.
- Target initial load ~200–400 KB Brotli. Heavy libs (Excel, charts, PDF, editors) are loaded with
  `await import()` inside the handler that needs them; no barrel files; import icons individually.
- Planned CI guardrails: `size-limit` budget on the entry chunks, dependency-cruiser rule
  restricting heavy libs to their feature folder.
