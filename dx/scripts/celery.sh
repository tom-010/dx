#!/usr/bin/env bash
# Run Celery against the dev Valkey (./scripts/db.sh starts it). Tasks reach the worker as long
# as CELERY_EAGER is not set to true (the default is a real worker, like in production).
#   ./scripts/celery.sh            # dev worker with auto-reload (default) — like runserver
#   ./scripts/celery.sh -- -c 4    # same, extra args go to `celery worker`
#   ./scripts/celery.sh worker     # plain worker, no reload (extra args go to celery: -c 4)
#   ./scripts/celery.sh beat       # periodic tasks (file-based schedule; see CELERY_BEAT_SCHEDULE)
#   ./scripts/celery.sh flower     # web UI on http://localhost:5555 (flower is fetched on demand)
#   ./scripts/celery.sh purge      # drop all queued tasks
#   ./scripts/celery.sh ping       # is a worker alive?
#
# `dev` = `manage.py celery_dev`: restarts the worker whenever a .py file under apps/ or config/
# changes (watchfiles). Restarts are warm: running tasks finish first (up to --stop-timeout
# seconds, then SIGKILL — a killed task comes back only after the broker's visibility timeout),
# reserved tasks go back to the queue. One process (--concurrency=1) keeps restarts fast.
#
# Like serve.sh, the long-running modes print to stdout AND to logs/celery.log (dev/worker) or
# logs/celery-beat.log (beat) in the repo root; the file starts fresh on every start.
set -euo pipefail
cd "$(dirname "$0")/../backend"
cmd="${1:-dev}"
case "$cmd" in --|-*) cmd=dev ;; *) [ $# -gt 0 ] && shift ;; esac

# Unbuffered, otherwise Python holds back output when stdout is a pipe (tee).
export PYTHONUNBUFFERED=1

logged() {  # logged <log name> <command...>: run, mirroring all output into logs/<name>
  local log
  log="$(cd .. && pwd)/logs/$1"
  shift
  mkdir -p "$(dirname "$log")"
  : > "$log"
  echo "logging to $log" >&2
  # -i: tee must survive the Ctrl+C that stops the worker, or its shutdown lines are lost.
  "$@" 2>&1 | tee -a -i "$log"
}

case "$cmd" in
  dev)     logged celery.log uv run python manage.py celery_dev "$@" ;;
  worker)  logged celery.log uv run celery -A config worker --loglevel=info "$@" ;;
  beat)    logged celery-beat.log uv run celery -A config beat --loglevel=info "$@" ;;
  flower)  exec uv run --with flower celery -A config flower "$@" ;;
  purge)   exec uv run celery -A config purge -f "$@" ;;
  ping)    exec uv run celery -A config inspect ping "$@" ;;
  *)       exec uv run celery -A config "$cmd" "$@" ;;
esac
