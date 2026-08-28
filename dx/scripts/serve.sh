#!/usr/bin/env bash
# Start the Django dev server + the auto-reloading Celery worker in this terminal (delegates to
# backend/scripts/serve.sh; HOST/PORT/WORKER env vars apply). Logs: stdout + logs/backend.log,
# logs/celery.log.
exec "$(dirname "$0")/../backend/scripts/serve.sh" "$@"
