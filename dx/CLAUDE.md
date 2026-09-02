# dx — project guide for Claude

Data-management app: Django backend + React SPA, later wrapped with Capacitor for Android/iOS.
Architecture decisions and rationale live in `NOTES.md` (currently German; the decisions there are binding).


## Conventions

- **Everything in English**: code, comments, docs, commit messages.
- Monorepo: `backend/` (Django, uv) and `frontend/` (Vite SPA, pnpm). One deployable in prod
  (Django serves `frontend/dist` via WhiteNoise); in dev both run separately.
- **Vocabulary**: a feature lives in a Django **app** under `apps/<name>/`. "App" is the only
  word for it — Django's own (`INSTALLED_APPS`, `AppConfig`, `app_label`), so a second name would
  just be a translation step. "Module" means a Python file and nothing else; "feature" means a
  capability, which may be one app or part of one.
- **One `api.py` per app** holds the ninja schemas, the logic and the router together. No
  `services.py`, no `schemas.py` — a lookup that 404s is three lines in the view, and the
  handful of functions two routes share sit above them in the same file.
- Every API operation declares `response=Schema`; never return bare dicts. The OpenAPI spec
  (`openschema.json`) is the contract with the frontend; the frontend talks to the API ONLY through
  the code orval generates from it (`frontend/src/api/`). See "End-to-end types".
- Decisions listed in `NOTES.md` §2 (stack table) are settled — do not swap libraries without asking.
- **Multitenancy, tenant == user**: every feature model is an `OwnedModel`; isolation is enforced
  by the ORM scope *and* by Postgres row-level security, never by hand-written filters alone.
  See `.claude/rules/multitenancy.md` — its invariants are not negotiable.
- **Everything is versioned and nothing is deleted**: every write to a feature model is mirrored
  into an append-only event table by a database trigger, and deletes are soft. See
  `.claude/rules/versioning.md` — those invariants are not negotiable either.
- **Every write states its lineage.** `Model.create(...)` and `obj.save(...)` **require**
  `operation=` and `sources=` — there are no defaults, and `Model.objects.create()` is refused
  outright (it cannot carry them). The row, its edges and its step land in one transaction:

  ```python
  report = Report.create(operation="convert to EUR", sources=[rates, totals], text=...)
  report.save(operation="rebuild report", sources=[rates])        # an edit, likewise
  note.save(operation=None, sources=[])                           # a person typed it: no step,
                                                                  # built from nothing
  with history_context("summarise"), deriving(doc):               # a block, for several writes
      Summary.create(operation=None, sources=None, text=...)      # None = "the block says it"
  ```

  `operation` is the **name of the step a data reviewer sees on the lineage node** — "summarise
  notes", "import dataset from document" — stable across runs; never "api"/"update" (the request
  context records that), never a code path (the edge stores the call stack), never anything about
  the data (it lands in a table every tenant can read). `None` when a human made the write, which
  is most of them. `operation_description=` is the optional longer form: what the step did *in
  this run* ("14 chunks, opus, prompt v3"). `sources` are the rows this one was **computed
  from** — the test is "if that row changed, would this have to be recomputed?" — not structural
  foreign keys (`owner`, a tag's dataset). Full guidance in `VersionedModel.save.__doc__`;
  the why in `docs/history_lineage_delete_tenants.md`. Every write also records **who made
  it**: `version.caller`/`.stack`/`.release` for a version, the same three on a lineage edge —
  and each frame's `sha` points at the whole function's source in `core.SourceSnippet`, stored
  once per distinct text (`apps/core/source.py`). A write through the API also records the
  request that made it — method, path, redacted headers, JSON body — once per request, in
  `core.RequestRecord` (`version.request_id`, `edge.request`; `apps/core/request_record.py`).
- **Soft delete in one line**: `obj.delete()` / `qs.delete()` are soft (a versioned UPDATE of
  `deleted_at`, no cascade), `obj.hard_delete()` / `qs.hard_delete()` really remove the row and
  its history (erasure, credential purging, test teardown only), `Model.objects` hides deleted
  rows, `Model.all_objects` includes them, and a raw `DELETE` is refused by the database
  trigger either way — `docs/soft-delete.md`.
- **Every model hands out one example of itself**: a static `example()` returning one filled-in,
  unsaved instance (a required FK is built from *that* model's `example()`), and
  `save_example(Note.example())` writes the whole tree (`save_deep(obj, operation=…, sources=…)`
  in `apps/core/save_deep.py` for any hand-built one) — `manage.py check_examples` proves every
  model still has a saveable one. Writing them: `.claude/skills/model-examples`.

## Where the detail lives

Everything below is always in context. The rest is in `.claude/rules/*.md`, each scoped with
`paths:` so it loads when you touch the matching files — `multitenancy`, `versioning`,
`backend-layout` (any `backend/` file), `frontend` (any `frontend/` file), `auth`,
`media-storage`, `celery`, `logging`, `backups`, `health-checks`, `admin`,
`management-commands`, `type-checking`, `testing`, `spa-serving`, `production`,
`notes-showcase`. Read the rule file directly when you need it before touching a file.

## Principles: Your approach to write code. Your attitude toward programming

Code is a Liability; Mutability is the Only Metric. Every line of code creates maintenance debt and entropy. The objective is maximum utility via minimum syntax. Static "quality" is irrelevant if the system resists modification; a rigid system that functions correctly is a failure. Therefore, subtraction is superior to addition, and explicit duplication is scientifically superior to premature abstraction. Wrong abstractions introduce invisible, high-cost dependencies that cripple future velocity.

The Bottleneck is Cognitive Capacity, Not Hardware. Software velocity is constrained by the developer's working memory, not CPU cycles. "Clever" code exhausts this resource; "boring," predictable code preserves it for domain logic. Enforce strict uniformity to eliminate decision fatigue regarding implementation details. Optimize for locality—co-locating related logic—to minimize context switching. Coupling is the primary enemy of cognitive containment; distinctness enables speed.

Scale is a Distraction; Architect Only for Now. Speculative architecture for hypothetical futures is resource waste. Solve strictly for the immediate reality (e.g., 10 users). Leverage "Lindy" technologies—proven standards like SQL and HTTP—where failure modes are known; novelty introduces unquantified risk. Speed today is a requirement; speed tomorrow is achieved not by generic flexibility, but by a disciplined refusal to couple components.

Value Follows a Power Law; Imperfection is Economic. The majority of utility derives from a minority of features. Perfectionism in the "long tail" or secondary UI is economic malpractice. Real-world usage is the only valid validation mechanism for the scientific method. Consequently, rapid, imperfect shipping outperforms perfect planning. A solution exists only when value is delivered; until then, it is merely inventory.

Other little ideas:
- The URL is the Source of Truth. The Database is the State. The Client is just a View. No API Layer, no client state like redux, no loading spinners.
- F5-ability: All UI state must survive a page refresh. Tabs, filters, modals, selections — if it's visible, it's in the URL. Never store UI state in React state alone.
  - **Search params (`?tab=settings`):** Use when the loader needs the value — tabs that load different data, filters, pagination, search queries. This is the default choice.
  - **Hash (`#section`):** Use for in-page scroll anchors or purely client-side state that doesn't affect data loading. Rare in this stack.
- Co-location is king: Put things together, best in a single file.
- No magic. E.g. route.ts over file-system based routing.
- Keep things simple. E.g. no own caching layer.
- Design for 10 users or less
- Types are your friend as they provide fast feedback for fast iterations.
- The Programmers Time, Brain-Capacity and Happyness are the most important resource.
- Logging: Unix philosophy. Silent on success, stderr on error. No emojis, no "cute" messages.
- The database is ephemeral. Seed data is the source of truth. Reset anytime via `npm run db:reset`.



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
| Find a command       | `cd backend && uv run python manage.py tui [WORDS…]` (fuzzy-find any command, read its `--help`, run it; recently used first — every run is logged to `core.CommandRun`; `--list` = plain table) |
| New command          | `cd backend && uv run python manage.py newcommand <name> [--app APP]` (scaffolds it in the app you pick; commands are not tenant-filtered — `all_tenants()`) |
| Tenant shell         | `cd backend && uv run python manage.py shell_as [-u USER \| --last]` (one user's data; picker with MRU + Tab completion) · `shell_admin --reason '…'` (all tenants, needs `DB_ADMIN_*`, audited) |
| One tenant's data    | `manage.py pull_tenant USER [-o FILE] [--no-scrub] [--with-files]` → scrubbed fixture (zip with the uploads) · `manage.py load_tenant FILE` · erase: see below; **`docs/tenant-data.md`** |
| Erase one tenant     | `DB_ROLE=migrator manage.py delete_tenant USER [-y]` (user + all owned rows + **their version history and lineage** + their files; the only working way to delete a user — the admin's delete button fails) |
| Tracked-field snapshot | `cd backend && uv run python manage.py history_schema [--write]` → `backend/history_schema.json`; run after any field change on a tracked model (see `.claude/rules/versioning.md`) |
| Maintenance worker   | `./scripts/celery.sh maintenance` (beat + `maintenance` queue as the table owner; the nightly backup runs here, not on the dev worker) |
| Backend tests        | `cd backend && uv run pytest`                               |
| Backend lint/format  | `cd backend && uv run ruff check . && uv run ruff format .` |
| Backend type-check   | `cd backend && uv run mypy .` (strict + django-stubs)       |
| API docs / spec      | http://127.0.0.1:8000/api/docs · `/api/openapi.json`        |
| Dev home page        | http://127.0.0.1:8000/ (DEBUG only): links to the docs, explorer, admin and the Vite server, behind the admin login, with a logout button (`config/home.py`) |
| Why any of this      | `docs/history_lineage_delete_tenants.md` (lineage → history → soft delete → tenancy, in that order) |
| What a write records | `docs/history_lineage_what_is_recorded.md` (version, edge, function source, request — every column and the mechanism behind it) |
| Soft delete API      | `docs/soft-delete.md` (delete, read, restore, cascade, the one escape hatch) |
| Lineage demo data    | `cd backend && uv run python manage.py lineage_demo [--clean]` (builds 11 lineage shapes out of ModelA/ModelB — chain, merge, split, diamond, feedback, rebuild, erased, hub, churn, restore, moving — and prints an explorer link per shape) |
| Lineage explorer     | http://127.0.0.1:8000/explorer/ (dev only, staff session): pick a user → models → rows → one row's versions and lineage (`apps/core/explorer.py`) |
| Health / readiness   | `curl http://127.0.0.1:8000/api/health` (liveness) · `/api/ready` (503 + failing checks; see `.claude/rules/health-checks.md`) |
| Frontend lint/format | `cd frontend && pnpm lint` / `pnpm format`                  |
| Frontend build       | `cd frontend && pnpm build` (runs `tsc -b` first)           |
| Add shadcn component | `cd frontend && pnpm dlx shadcn@latest add <name>`          |
| Sync schema + client | `./scripts/sync_schema.sh` = `frontend/sync_schema.sh` (`--check`, `--watch`) |
| Dev superuser        | `cd backend && uv run python manage.py createadmin` (admin/admin) |
| Token for curl       | `TOKEN=$(cd backend && uv run python manage.py token)` then `curl -H "Authorization: Bearer $TOKEN" …` (expires after `ACCESS_TOKEN_LIFETIME_MINUTES`; `-m 60` for longer) |
| Backend tests (short)| `./scripts/test.sh [pytest args]` (parallel, 8 workers; `--reuse-db` for faster re-runs, `PYTEST_WORKERS=0` for serial/pdb) · `./scripts/coverage.sh [--open]` |
| Format everything    | `./scripts/format.sh` (ruff + Biome, auto-fix)               |
| Lint everything      | `./scripts/lint.sh` (ruff, mypy, Biome — no changes)         |
| Type-check everything| `./scripts/check.py [backend\|frontend]` (mypy strict + django-stubs, `tsc -b`); see `.claude/rules/type-checking.md` |
| CI in one command    | `./scripts/ci.py [backend\|frontend\|image]` = `check.py` + `./scripts/build.sh` (production image `dx-app:latest`) |
| Full pre-commit check| `./scripts/check.sh` (lint + pytest + build + sync_schema --check; ~16s: parallel tests, reused test DBs, rebuilt automatically when a migration changes) |
| DB backup / restore  | `./scripts/backup.sh [--list\|--prune]` (= `DB_ROLE=migrator manage.py backup` → `dx-backups` bucket or `backend/backups/`), `./scripts/restore.sh <name>\|--latest [-y]`, `./scripts/roundtrip.sh` (dev only, drops the DB); see `.claude/rules/backups.md` |
| New feature app      | `cd backend && uv run python manage.py newapp <name> [--model Item]` (scaffold + register + makemigrations; see `.claude/rules/backend-layout.md`) |
| Delete a feature app | `cd backend && DB_ROLE=migrator uv run python manage.py deleteapp <name>` (the opposite: drops its tables **and their history**, unregisters it, deletes `apps/<name>/`; `--keep-data` keeps the tables) |
| Versioning/lineage demo | `/notes` in the SPA → edit a note, merge two, then "History" / "Lineage" (`apps/notes`, a showcase — see its section) |
| Reference command    | `cd backend && uv run python manage.py hello_world [NAME] --shout` (django-click + rich; see `.claude/rules/management-commands.md`) |
| Model examples       | `cd backend && uv run python manage.py check_examples` (every model's `example()` exists and saves; run by `check.sh`) |
| Build production image | `./scripts/build.sh` → `dx-app:latest` (`--run` also starts it on :8080 via the dev compose `app` profile, plain http) |
| Production stack     | `./scripts/prod.sh` (= `docker compose --env-file docker/.env.prod -f docker/docker-compose.prod.yml …`; no args = `up -d --wait`); see `.claude/rules/production.md` |
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

## Invariants (never violated, never worked around)

Full reasoning in `.claude/rules/multitenancy.md` and `.claude/rules/versioning.md`; both load
on any `backend/` file. The short form, because breaking these is not recoverable by review:

- **Tenant == user.** Feature models extend `OwnedModel` (`owner` FK); read them through
  `Model.objects.for_user(user)`. Isolation is the ORM scope *plus* Postgres RLS — never a
  hand-written `.filter(owner=…)` as the only guard. Writes need a tenant context: requests get
  it from `TenantMiddleware`, tests from `with acting_as(user):`, tasks from `@tenant_task`
  (never `@shared_task` in a tenant app). Migrations run via `./scripts/migrate.sh`, which syncs
  the RLS policies — plain `manage.py migrate` fails.
- **Everything is versioned, nothing is hard-deleted.** Concrete models are `@tracked`; a
  database trigger mirrors every write into an append-only event table. Never assign `id`,
  `created`, `modified` or `version` in Python — all four are database/trigger set. `delete()` is
  soft; `hard_delete()` is the exception (erasure, credential purging, test teardown) and a raw
  `DELETE` is refused by the database either way. Every unique constraint is conditioned on
  `deleted_at__isnull=True`, and cascade is application logic (Django's collector never runs).
- **Every write says where the row came from.** `operation=` and `sources=` are required on
  `Model.create()` and `save()` (see above); `Model.objects.create()` raises. A write that
  records nothing says so — `sources=[]` — so it is a decision in the diff, not an omission.

## Naming

- **App** = plural of its model where a list page of that word makes sense (`datasets`/`Dataset`);
  otherwise the domain noun, singular, with the unit as the model (`gallery`/`MediaItem`,
  `accounts`/`User`). An awkward plural ("anamneses", "deep researches") means the second case.
- **Model** singular CamelCase, no underscores; schemas `<Model>Out`/`In`/`Patch`,
  `Paged<Model>Out`. Model names are a **global namespace**: the revision/lineage URL segment is
  the lower-cased model name (`/history/dataset/<id>`), unique across apps (`test_history.py`).
- One concept, three namespaces, three idioms: app label `deep_research` → REST-style
  `/api/deep-research` → `frontend/src/routes/deep-research.tsx`. Path params are snake_case
  (`{dataset_id}`).
- Taken words, do not reuse for a feature: `history`/`Event` (versioning), `lineage`, `core`,
  `tasks`, `owner`, `scope`.

## Data model conventions (`apps/core/models.py`, `apps/core/schemas.py`)

- **Primary keys are UUIDv7** (`VersionedModel.id`, `db_default=Func(function="uuidv7")`, PG 18):
  time-ordered like an auto-increment id (index locality, sortable by creation), globally unique,
  and cheap to generate anywhere so offline-created rows never collide (NOTES.md §6). Native
  Postgres `uuid` column, and the default lives in the *database* so a raw INSERT or a data
  migration gets a well-formed id too (never set `default=` as well — Django would silently
  ignore `db_default`). We are deliberately locked
  in on Postgres. Ids are `NewType`s per model (`DatasetId = NewType("DatasetId", uuid.UUID)`),
  ninja path params are `uuid.UUID`, the generated TS types have `id: string`.
- **Every model extends `VersionedModel`** (id, `created`, `modified`, `version`, `deleted_at`,
  `soft_delete()`, `set_payload()` for PUT, `set_payload_partial()` for PATCH — both pass values
  through as they are on the schema, so typed JSON fields keep their pydantic instances).
  `accounts.User` carries the same UUIDv7 id. The first four columns are set by the database
  (`db_default` / the `bump_version` trigger) and must never be assigned in Python; see
  `.claude/rules/versioning.md`.
- **User data extends `OwnedModel`** (`owner` FK to `AUTH_USER_MODEL`, reverse accessor
  `user.<models>`) and is only read through `Model.objects.for_user(user)` (`OwnedQuerySet`,
  which also hides soft-deleted rows; `Model.all_objects` keeps the tenant scope and includes
  them) —
  the explicit half; `OwnedManager` adds the ambient tenant scope and Postgres RLS the
  guarantee (see `.claude/rules/multitenancy.md`). Services take the acting `User` first
  (`create_dataset(user, name=...)`, `get_dataset(user, id)`) and set `owner=user`; foreign
  rows raise the app's `*NotFound` → 404, never 403. `apps/core/tests/test_ownership.py`
  runs the HTTP isolation contract (empty list, 404 on get/put/patch/delete) for every resource
  in `RESOURCES` — register new owned apps there; `test_tenancy.py` covers the model layers
  automatically. Signed links (`/media/…?sig=`, downloads) are the one exception: the
  signature stands in for the user (download links carry the owner id and open their context).
- **Input schemas extend `StrictSchema`** (`extra="forbid"`): unknown fields are a 422 instead of
  being ignored (a typo in a PATCH would otherwise be a silent no-op). Output schemas stay
  `Schema`/`ModelSchema`. All of an app's schemas live in its `api.py`, next to the routes that
  use them.
- **Typed JSON columns**: `django_pydantic_field.SchemaField(Model, default=Model)`
  (`Dataset.options: DatasetOptions`) — stored as `jsonb`, validated on load and save, and a
  nested typed object in the API/TS types (NOTES.md §5). Give new fields defaults so old rows
  still load; put `extra="forbid"` on the pydantic model as well.
- **Lists are paginated** with ninja's `@paginate(PageNumberPagination)`: `?page=&page_size=`
  (`NINJA_PAGINATION_PER_PAGE=50`, max 500) → `{"items": [...], "count": n}` (`PagedDatasetOut`,
  `useListDatasets({ page })`). Services return the `QuerySet`; the view paginates it.
- Every app exposes the same surface: paginated `GET /x`, `POST /x` (201),
  `GET/PUT/PATCH/DELETE /x/{id}`. `apps/datasets` is the reference; `newapp` reproduces it —
  including the lineage keywords on every write (`operation=None, sources=[]` for the CRUD
  views, since a row the user typed is no step and is derived from nothing).
