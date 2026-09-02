---
paths:
  - "**/docker/**"
  - "**/scripts/build.sh"
  - "**/scripts/prod.sh"
---

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
  `.claude/rules/multitenancy.md`).
- Also `redis` (Valkey, :6379, append-only persistence in volume `valkeydata` — see
  `.claude/rules/celery.md`) and `s3` (RustFS, :9100/:9101, volume `s3data` — see `.claude/rules/media-storage.md`).
  `./scripts/db.sh` waits until every healthcheck passes, then runs `ensure_bucket`.
  Reset everything (DB + queue + uploads): `./scripts/db.sh down -v`.
