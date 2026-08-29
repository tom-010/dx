# dx — project guide for Claude

Data-management app: Django backend + React SPA, later wrapped with Capacitor for Android/iOS.
Architecture decisions and rationale live in `NOTES.md` (currently German; the decisions there are binding).

## Conventions

- **Everything in English**: code, comments, docs, commit messages.
- Monorepo: `backend/` (Django, uv) and `frontend/` (Vite SPA, pnpm). One deployable in prod
  (Django serves `frontend/dist` via WhiteNoise); in dev both run separately.
- Business logic lives in a typed service layer without framework imports; ninja routers stay thin.
- Every API operation declares `response=Schema`; never return bare dicts. The OpenAPI spec
  (`openschema.json`) is the contract with the frontend; the frontend talks to the API ONLY through
  the code orval generates from it (`frontend/src/api/`). See "End-to-end types".
- Decisions listed in `NOTES.md` §2 (stack table) are settled — do not swap libraries without asking.

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
| Django management    | `cd backend && uv run python manage.py <cmd>`               |
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
| DB backup / restore  | `./scripts/backup.sh [--list\|--prune]` (= `manage.py backup` → `dx-backups` bucket or `backend/backups/`), `./scripts/restore.sh <name>\|--latest [-y]`, `./scripts/roundtrip.sh` (dev only, drops the DB); see "Backups" |
| New feature module   | `cd backend && uv run python manage.py startmodule <name> [--model Item]` (scaffold + register + makemigrations; see "Backend") |
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
  - `apps/<feature>/` — one Django app per feature module: `api.py` (ninja `Router`),
    `models.py`, services, `tests/test_*.py`. Register new apps in `INSTALLED_APPS` as
    `"apps.<feature>"` and their router in `config/api.py`.
  - `apps/core/` — infrastructure: `health.py` (`GET /api/health`, `GET /api/ready`, see "Health
    checks"), `models.py` (`BaseModel`: UUIDv7 pk + `created`/`modified` +
    `set_payload()`/`set_payload_partial()` for PUT/PATCH; `OwnedModel` + `OwnedQuerySet.for_user()`
    — see "Data model conventions"), `schemas.py` (`StrictSchema` for inputs), `backups.py`
    (see "Backups"), `scaffold.py` + `backend/scaffold/module/` (`manage.py startmodule`),
    `storage.py` (buckets), the sample Celery tasks (`tasks.py`, `services.py`, `/api/tasks/...`
    in `api.py`, tag `tasks`) and the cross-cutting tests (`tests/test_security.py`,
    `test_openapi.py`, `test_errors.py`, `test_ownership.py`).
  - `apps/accounts/` — authentication, see "Auth" below.
  - `apps/datasets/` — demo feature module and the reference for new ones (`startmodule`
    generates the same shape): `models.py` (`Dataset(OwnedModel)`, `DatasetId = NewType(...,
    uuid.UUID)`, `DatasetOptions` = typed JSON column), `schemas.py` (`DatasetOut`, `DatasetIn`,
    `DatasetPatch`), `services.py` (plain typed functions taking the acting `user`, raise domain
    exceptions such as `DatasetNotFound`), `api.py` (router maps exceptions to `HttpError`,
    returns `Status(201, obj)` for non-200 codes), `admin.py`, `tests/test_api.py`.
    Endpoints: paginated `GET /api/datasets`, `POST /api/datasets`,
    `GET/PUT/PATCH/DELETE /api/datasets/{id}`.
  - `apps/documents/` — file uploads: `Document(OwnedModel)` (`FileField` on Django's default
    storage = the S3-compatible object store, see "File storage"; keys `documents/%Y/%m/<name>`),
    multipart `POST /api/documents/upload` (`files: File[list[UploadedFile]]`, validated as a
    batch in `services.py`), paginated list/get/delete, and `GET /api/documents/{id}/download`
    (streams a `FileResponse`; exposed to clients as `DocumentOut.download_url`, so storage can
    change without touching the frontend; the signed link replaces the user check —
    `get_document_for_download`).
  - `apps/gallery/` — images and videos shown inline: `MediaItem` (owned; `kind` image|video,
    `file` under `gallery/%Y/%m/`), `services.py` decides the MIME type (browser's, else guessed
    from the name), rejects everything that is not `image/*`/`video/*` and enforces per-kind
    size limits (`MAX_SIZE`). `POST /api/gallery/upload`, paginated `GET /api/gallery`, get/delete.
    `MediaItemOut.url` is the signed `/media/…` link (see "Media files") — the SPA puts it
    straight into `<img src>` / `<video src>`.
  - `config/spa.py` + `config/static.py` — serve the built SPA (see "Serving the SPA");
    `config/media.py` — storage classes + `/media/<key>?sig=` view (see "Media files").
  - `config/errors.py` — JSON error bodies (`{"detail": …}`) for API clients: Django's
    handler400/403/404/500 (`urls.py`) and a ninja exception handler that, with `DEBUG=true`,
    returns `{"detail": "Type: msg", "traceback": [...]}` for unhandled exceptions (in prod
    they propagate to Django's logging + `handler500`).
- Config: defaults in `config/env.py` target the compose database
  (`postgres://dx:dx@localhost:5432/dx`). Override via env vars or `backend/.env`
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
  `apps/<name>/` from `backend/scaffold/module/` (owned model with `name`/`description`,
  `schemas.py` Out/In/Patch, services taking `user`, router with paginated list +
  get/POST/PUT/PATCH/DELETE, admin, tests), inserts it at the `# needle:` comments in
  `settings.py` (`INSTALLED_APPS`) and `config/api.py`, runs `makemigrations` + ruff. Then:
  adjust `models.py`/`schemas.py`, add the resource to `RESOURCES` in
  `apps/core/tests/test_ownership.py`, `./scripts/sync_schema.sh` (view names become hook
  names), frontend route + nav entry. Logic in `apps/core/scaffold.py` (tested on temp copies).

## Data model conventions (`apps/core/models.py`, `apps/core/schemas.py`)

- **Primary keys are UUIDv7** (`BaseModel.id`, `default=uuid.uuid7`, Python 3.14): time-ordered
  like an auto-increment id (index locality, sortable by creation), globally unique, and
  generated client-side so offline-created rows never collide (NOTES.md §6). Native Postgres
  `uuid` column; PG 18's `uuidv7()` yields the same ids in raw SQL. We are deliberately locked
  in on Postgres. Ids are `NewType`s per model (`DatasetId = NewType("DatasetId", uuid.UUID)`),
  ninja path params are `uuid.UUID`, the generated TS types have `id: string`.
- **Every model extends `BaseModel`** (id, `created`, `modified`, `set_payload()` for PUT,
  `set_payload_partial()` for PATCH — both pass values through as they are on the schema, so
  typed JSON fields keep their pydantic instances). `accounts.User` carries the same UUIDv7 id.
- **User data extends `OwnedModel`** (`owner` FK to `AUTH_USER_MODEL`, reverse accessor
  `user.<models>`) and is only read through `Model.objects.for_user(user)` (`OwnedQuerySet`).
  Services take the acting `User` first (`create_dataset(user, name=...)`,
  `get_dataset(user, id)`); foreign rows raise the module's `*NotFound` → 404, never 403.
  `apps/core/tests/test_ownership.py` runs the isolation contract (empty list, 404 on
  get/put/patch/delete) for every resource in `RESOURCES` — register new owned modules there.
  Signed links (`/media/…?sig=`, downloads) are the one exception: the signature stands in for
  the user.
- **Input schemas extend `StrictSchema`** (`extra="forbid"`): unknown fields are a 422 instead of
  being ignored (a typo in a PATCH would otherwise be a silent no-op). Output schemas stay
  `Schema`/`ModelSchema`. When services need the payload objects (PUT/PATCH) the schemas live in
  `apps/<feature>/schemas.py`, otherwise in `api.py`.
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

- Every API operation requires `Authorization: Bearer <token>` — `BearerAuth` (`auth.py`,
  ninja `HttpBearer`) is installed globally in `config/api.py`. Public operations opt out with
  `auth=None` (health/ready, login, register, signed document downloads, `/api/docs`). Outside the
  API, `/media/<key>?sig=` is public-but-signed (see "Media files").
- Three token kinds, all resolved by `services.authenticate_bearer()`:
  1. JWT access tokens (HS256 with `SECRET_KEY`, claim `token_type=access`, stateless and
     therefore short-lived: `ACCESS_TOKEN_LIFETIME_MINUTES`, 15) from `POST /api/auth/login`
     (`{username, password}` → `{access_token, refresh_token}`). The refresh token is a second
     JWT (`token_type=refresh`, `REFRESH_TOKEN_LIFETIME_DAYS`, 30) whose `jti` is a
     `RefreshToken` row = the login session. `POST /api/auth/refresh` (`{refresh_token}`,
     public — the access token is expired by then) trades it for a new pair and revokes the
     old row (single-use); `POST /api/auth/logout` (`{refresh_token}`, public, always 204)
     revokes it. Expired/revoked/inactive user → 401 "Invalid or expired refresh token".
     Deliberately no reuse detection (ending every session when an old token shows up — two
     tabs refreshing at once would trigger it; `services.rotate_refresh_token`). Login purges
     the user's expired rows. `GET /api/auth/me` returns the caller.
  2. Personal API tokens (`tk_…`, `ApiToken` model, never expire) for scripts/CI:
     `GET/POST /api/auth/api-tokens`, `DELETE /api/auth/api-tokens/{id}`.
  3. `API_FIXED_TOKEN` from the environment → acts as the first superuser (CI without DB state).
- `POST /api/auth/register` only works with `REGISTRATION_OPEN=true` (403 otherwise).
- Inside operations use `current_user(request)` (`auth.py`); services take a `User`, never a
  request, and scope owned data with it (see "Data model conventions").
- Files that a browser fetches via plain `<a href>`/`<img src>` (no header) use signed, expiring
  URLs: `DocumentOut.download_url` carries `?sig=` (`documents/services.py::sign_download`),
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
  same signing idea, but forces a download with the original file name. Use `/media/…` for
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
- Keys are `upload_to` + the upload name (`documents/%Y/%m/<name>`); `file_overwrite=False`
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
- Pattern (`apps/core/tasks.py`): logic in `services.py` as plain functions; `tasks.py` holds thin
  `@shared_task` wrappers (no logic); an endpoint enqueues (`task.delay(...)`) and returns
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
  `backup_database`. Run the scheduler with `./scripts/celery.sh beat` (the prod stack has a
  `beat` service).
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

- `manage.py backup` (`./scripts/backup.sh`) dumps the database with `dumpdata` (natural keys;
  no content types/permissions/sessions/log entries) into `dx-<UTC timestamp>.json.gz` in the
  **`backups` storage** (`STORAGES["backups"]`): the `S3_BACKUP_BUCKET` bucket (`dx-backups`,
  versioned, created by `ensure_bucket`), or `backend/backups/` with `MEDIA_STORAGE=local`.
  `--list` shows the dumps, `--prune` keeps only the newest `BACKUP_KEEP` (30).
- `manage.py restore <name>|--latest [-y]` (`./scripts/restore.sh`) = `migrate` + `loaddata`:
  rows with the same pk are overwritten, nothing is deleted. `./scripts/roundtrip.sh` proves it
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
  `celery` (broker reachable; "eager mode" when tasks run inline), `storage:default` and
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
  `startmodule`, `hello_world` (core).
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
  return annotated, no `Any` in them) and, for `apps/*/services.py`, `disallow_any_explicit`:
  business logic is `Any`-free (`UploadedFile[bytes]`, not `[Any]`); framework edges
  (settings, env, task meta) may still use `Any`.
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

1. **Service tests** (`test_services.py`, or inside `test_api.py` when small): call the plain
   functions in `services.py` directly — domain rules, validation, exceptions. Most cases live
   here; no HTTP, no auth.
2. **API tests** (`test_api.py`, auto-marked `api`): the HTTP contract through Django's test
   client — status codes, JSON shape, error `detail`, one happy path per endpoint. Use
   `auth_client`; `client_for(other_user)` for ownership/isolation ("B gets 404 for A's
   things"); the anonymous `client` only for public endpoints and 401 checks.
3. **Cross-cutting guarantees** (`apps/core/tests/`): `test_security.py` — every operation is
   authenticated unless listed in `PUBLIC_OPERATIONS` with a reason, plus an automatic anonymous
   request against every path in the spec (must be 401), docs need a staff session;
   `test_openapi.py` — operation ids unique/snake_case and `openschema.json` in sync with the
   code; `test_errors.py` — JSON error bodies; `test_ownership.py` — other users get an empty
   list and 404s for every owned resource in `RESOURCES`; `test_models.py` — UUIDv7 keys,
   `for_user`, payload helpers; `test_schemas.py` — `StrictSchema`.
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
  `auth_client`. Create data through services (`create_dataset(user, ...)`), not raw ORM calls.
- Markers are registered in `pyproject.toml` (`--strict-markers`): `api`, `infra`, `slow`.
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
  `MIGRATE_ON_START=false`) `ensure_bucket` + `migrate` → exec. Worker and beat run the same
  image with a different command and start once `app` is healthy.
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

- `postgres:18-alpine`, user/password/db = `dx`/`dx`/`dx`, port 5432, named volume `pgdata`
  mounted at `/var/lib/postgresql` (Postgres 18 image layout). No pgbouncer in dev.
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
    and video uploads rendered inline from their signed `url`).
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
