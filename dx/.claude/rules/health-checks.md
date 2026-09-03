---
paths:
  - "**/backend/apps/core/health.py"
---

## Health checks (`apps/core/health.py`)

- `GET /api/health` — liveness: the process answers requests; touches nothing (a database outage
  must not get the container restarted). Body `{"status": "ok"}`.
- `GET /api/ready` — readiness: `database` (`SELECT 1`; detail = host:port and whether the
  process is `pooled` through PgBouncer or `direct` — the web process must say pooled),
  `migrations` (nothing unapplied),
  `rls` (every owned table has its policy, and the connection's role is subject to it — a
  process connected as owner/superuser/`BYPASSRLS` is not ready), `celery` (broker reachable;
  "eager mode" when tasks run inline), `storage:default` and
  `storage:backups` (buckets exist; "local disk" otherwise). Body
  `{"status": "ok"|"fail", "checks": [{name, ok, detail}]}`, HTTP **503** when any check fails —
  compose/Docker health checks and load balancers gate on this one. Both are public
  (`PUBLIC_OPERATIONS`). The home page (`routes/index.tsx`) shows both; `useReady()` reads the
  503 body from the `ApiError`.
