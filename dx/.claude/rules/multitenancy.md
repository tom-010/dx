---
paths:
  - "**/backend/**"
---

## Multitenancy (tenant == user; `apps/core/db.py`, `rls.py`, `middleware.py`, `checks.py`)

One database, one schema, row-level isolation enforced **twice** — an application bug must not
be able to leak data:

| Layer | Where | Role |
|---|---|---|
| Data model | `OwnedModel.owner` FK (the guide's `TenantModel`) | scoping and per-tenant extraction are mechanical |
| ORM guard | `OwnedManager` on django-scopes' state (`scope`, `scopes_disabled`) | a query outside a tenant scope **raises** `ScopeError`; inside it is filtered |
| **Database guard** | **Postgres row-level security**, policy `tenant_isolation` on every table with an `owner_id` column — owned models, the event tables holding their history, `Lineage` and `RequestRecord` (`apps/core/rls.py::isolated_models`, `manage.py rls_sync`) | **the guarantee**: `owner_id = NULLIF(current_setting('app.user_id', true), '')::uuid` for `USING` and `WITH CHECK`; no context → nothing visible or writable (fails closed) |
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
  base would break `makemigrations` for any longer app name). `bulk_create` skips `save()`,
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
  `load_tenant FILE` (pins the fixture's user; `pull_tenant --with-files` adds the uploaded
  objects and `load_tenant` restores them to the same keys — `docs/tenant-data.md`),
  `delete_tenant USER` (erasure: every owned row,
  **its version history and lineage edges**, and the files they reference — rows in one
  transaction and files only after it commits; `apps/core/tenants.py`). Both tools read through
  `tenants.owned_rows()` (`_base_manager`), so soft-deleted rows are exported and erased too: an
  export that hid them would be a false answer to "what do you hold about me", and history that
  outlived an erased user would defeat the erasure. Erasure is the one caller allowed to really
  delete (`history.hard_delete()`). Erasure needs cross-tenant credentials, which is also why
  **deleting a user from the admin does not work**: the cascade cannot see the owned rows, so it
  reports success and then fails at the foreign key on commit. `apps/core/scrub.py`: `PII_FIELDS` allowlist —
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
  (`owned_upload_path`), links stay signed; the Django admin reads owned models only
  inside a tenant context it opens itself (`AdminTenantMiddleware`, see `.claude/rules/admin.md`) and is off in
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
