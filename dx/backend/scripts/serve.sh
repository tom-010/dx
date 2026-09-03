#!/usr/bin/env bash
# Start the Django dev server on http://127.0.0.1:8000/ (the Vite dev server proxies /api here)
# AND the auto-reloading Celery worker (scripts/celery.sh) in this one terminal. Ctrl+C stops
# both; when one of them exits (e.g. "That port is already in use.") the other is stopped too.
#   HOST=… PORT=… ./scripts/serve.sh   # bind address (default 127.0.0.1:8000)
#   WORKER=0 ./scripts/serve.sh        # Django only (run scripts/celery.sh elsewhere)
#   REMAP_SIGTERM= ./scripts/serve.sh  # warm worker shutdown instead of the cold one below
# Output is prefixed with [django] / [worker]; unprefixed copies go to logs/backend.log and
# logs/celery.log (repo root; each file starts fresh on every start).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(cd .. && pwd)"
LOG="$ROOT/logs/backend.log"
mkdir -p "$ROOT/logs"
: > "$LOG"
echo "logging to $LOG" >&2
# Unbuffered, otherwise Python holds back output when stdout is a pipe.
export PYTHONUNBUFFERED=1

# Job control: each background job below becomes its own process group, so it can be stopped as
# a whole (`kill -INT -- -<pgid>`), and a terminal Ctrl+C reaches only this script, which then
# forwards it. SIGINT (not TERM) so that runserver, the worker reloader and `tee -i` all shut
# down in order and the last log lines are not lost.
set -m

prefix() {  # prefix TAG — label every line; survive the SIGINT sent to the job's group
  trap '' INT
  exec sed -u "s/^/$1 /"
}

pids=()
# The dev server connects through PgBouncer, as gunicorn does in the image (DB_POOLED,
# config/env.py); the worker below keeps a direct connection — it pins its tenant per session.
{ DB_POOLED=true uv run python manage.py runserver "${HOST:-127.0.0.1}:${PORT:-8000}" 2>&1 \
    | tee -a -i "$LOG" | prefix "[django]"; } &
pids+=("$!")
if [ "${WORKER:-1}" != "0" ]; then
  # Cold worker shutdown: Ctrl+C (and every reload restart) drops the running task instead of
  # waiting up to --stop-timeout for it; the task goes straight back onto the queue. Celery reads
  # REMAP_SIGTERM (billiard) and handles the reloader's SIGTERM as SIGQUIT.
  { REMAP_SIGTERM="${REMAP_SIGTERM-SIGQUIT}" "$ROOT/scripts/celery.sh" 2>&1 | prefix "[worker]"; } &
  pids+=("$!")
fi

stopped=0
stop() {
  [ "$stopped" = 1 ] && return
  stopped=1
  for pid in "${pids[@]}"; do kill -INT -- "-$pid" 2>/dev/null || true; done
}
trap stop INT TERM

# The first job to end decides the exit code (Ctrl+C: the interrupted wait's 130).
wait -n "${pids[@]}" && status=0 || status=$?
stop
wait "${pids[@]}" || true
exit "$status"
