---
paths:
  - "**/tasks.py"
  - "**/backend/config/celery.py"
  - "**/backend/apps/core/worker_reload.py"
  - "**/frontend/src/routes/tasks.tsx"
  - "**/scripts/celery.sh"
---

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
  worker runs in its own session; Ctrl+C or `kill` on the reloader stops both.
  `REMAP_SIGTERM=SIGQUIT` (exported by `scripts/serve.sh` for its worker, so Ctrl+C there is
  instant) makes Celery read that SIGTERM as a cold shutdown instead: the worker exits at once
  and re-queues the running task, which the next worker runs again from the start. Without a worker,
  enqueued tasks simply wait in Valkey.
- **Eager mode** (`CELERY_EAGER=true`, opt-in): tasks run inline in the caller, no worker needed
  — but no progress events either. Failures are stored on the result (`state=FAILURE` +
  exception) instead of raising into the request (`CELERY_TASK_EAGER_PROPAGATES=False`), so the
  API behaves the same as with a worker. Tests always run eager with an in-memory result store
  (`config/settings_test.py`).
- Pattern (`apps/core/tasks.py`): the task body is the work — **`@tenant_task` for anything
  that touches owned data**
  (first argument `owner_id`, see `.claude/rules/multitenancy.md`); an endpoint enqueues (`task.delay(...)`) and returns
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
