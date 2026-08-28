#!/bin/sh
# Container entrypoint (docker/Dockerfile): refuse an unsafe configuration, wait for the
# database, prepare it for the web process, then run the container command — gunicorn by
# default, `celery -A config worker|beat` for the other services.
#   MIGRATE_ON_START=false   skip bucket creation + migrations here (several web replicas: run
#                            `manage.py migrate` once as a release step instead).
set -eu

# Django's deployment checklist: SECRET_KEY strength, ALLOWED_HOSTS, HTTPS settings, mailer, …
# (config/settings.py). Warnings count as errors: a misconfigured container must not serve.
python manage.py check --deploy --fail-level WARNING

attempts=0
until python manage.py shell -c "from django.db import connection; connection.ensure_connection()" >/dev/null 2>&1; do
  attempts=$((attempts + 1))
  if [ "$attempts" -ge 60 ]; then
    echo "database not reachable after ${attempts} attempts, giving up" >&2
    exit 1
  fi
  echo "waiting for the database..." >&2
  sleep 1
done

# Only the web process prepares the database; workers start once it is healthy (compose).
if [ "${1:-}" = "gunicorn" ] && [ "${MIGRATE_ON_START:-true}" = "true" ]; then
  python manage.py ensure_bucket
  python manage.py migrate --noinput
fi
exec "$@"
