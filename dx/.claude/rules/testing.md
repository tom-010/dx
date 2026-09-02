---
paths:
  - "**/tests/**"
  - "**/test_*.py"
  - "**/conftest.py"
  - "**/scripts/test.sh"
  - "**/scripts/coverage.sh"
---

## Testing

Strategy: **fast, hermetic, layered; every route private by default; contracts enforced by
tests, not by review.** Backend: pytest + pytest-django against the dev Postgres (`test_dx`
database, created per run), settings `config/settings_test.py` (= real settings + eager Celery
with in-memory results + MD5 password hasher). Run `./scripts/test.sh`, coverage with
`./scripts/coverage.sh`.

**Speed.** `./scripts/test.sh` runs pytest-xdist with 8 workers, each with its own test database
(`test_dx_gw0`…): ~8s against ~11s serial for the current 504, measured on a 24-core machine.
The gap widens with the suite, because the fixed part — importing Django and collecting, in
every worker — is ~4.5s and everything above it divides. More workers do not help: these tests
wait on Postgres round-trips rather than compute (workers sit at ~75% CPU, the database at 1.6
of 24 cores), and 8, 12 and 16 workers land within a second of each other. `PYTEST_WORKERS=0`
gives a serial run, which is what `-s`, `--pdb` and a confusing failure want.

`./scripts/check.sh` adds `--reuse-db` and keeps the databases between runs. That is only sound
while the schema has not moved, so it fingerprints every migration file into
`backend/.pytest-db-stamp` and switches to `--create-db` when the hash changes — running
`--reuse-db` against a stale schema fails in ways that read like real bugs. Worth ~0.5s today
and more as migrations accumulate. `ci.py` starts from nothing.

Per test the overhead is already small — ~1.4 ms for the database fixture and the rollback — so
a slow test is a test that does slow things. Two dead ends, measured, so nobody repeats them:
relaxing Postgres durability (`synchronous_commit=off`) buys ~5%, because the tests roll back
instead of committing; and `--no-migrations` would build a schema with none of the triggers or
RLS policies, which is not the database we ship.

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
   owned model; `test_history.py` — every `VersionedModel` is tracked or exempt, triggers survive
   `Meta` inheritance, bulk and raw writes are versioned, event tables are append-only, soft
   delete is a version, unique constraints are conditional, the schema log is current, plus the
   lineage and revision-page behaviour end to end;
   `test_models.py` — UUIDv7 keys, `for_user`, payload helpers; `test_schemas.py`
   — `StrictSchema`.
4. **Infra** (`test_commands.py`, auto-marked `infra`): management commands (invoked with
   `click.testing.CliRunner`, see `.claude/rules/management-commands.md`) and dev tooling. `test_deploy.py`
   (marked `infra` explicitly) runs `check --deploy` in a subprocess with a production
   environment — the settings must pass Django's checklist or the image refuses to start.
   `test_env.py` covers the `config/env.py` translations (`DATABASE_URL`, `EMAIL_URL`) and guards.
5. **Frontend** (planned per NOTES.md §2, not set up yet): Vitest + Testing Library for
   components/feature code (mock the generated hooks, never `fetch`), Playwright for a few e2e
   flows (login, one CRUD, one task) against the bundled image.

Conventions:

- Fixtures live in `backend/conftest.py`: `user`, `other_user`, `staff_user`, `client_for`,
  `auth_client`. Create data through the module's own functions
  (`create_dataset_for(user, ...)` from `apps/datasets/api.py`) when the test is about that
  function, and `save_example(Dataset.example())` (`apps/core/examples.py`, skill
  `model-examples`) for a row that merely has to exist — not raw ORM calls,
  and inside `with acting_as(user):` (`apps.core.testing`) — writes need the tenant context
  (see `.claude/rules/multitenancy.md`); requests get it from the middleware. Every database test runs as the
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
