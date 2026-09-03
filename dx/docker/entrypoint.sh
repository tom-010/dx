#!/bin/sh
# Container entrypoint (docker/Dockerfile): refuse an unsafe configuration, wait for the
# database, prepare it for the web process, then run the container command — gunicorn by
# default, `celery -A config worker|beat` for the other services.
#   MIGRATE_ON_START=false   skip bucket creation + migrations here (several web replicas: run
#                            the migrate → rls_sync → rls_sync --check sequence once as a release
#                            step instead, as DB_ROLE=migrator).
set -eu

# Django's deployment checklist: SECRET_KEY strength, ALLOWED_HOSTS, HTTPS settings, mailer, …
# (config/settings.py). Warnings count as errors: a misconfigured container must not serve.
python manage.py check --deploy --fail-level WARNING

# The web process connects through PgBouncer (DB_POOLED), whose password check needs Postgres
# as well, so one successful connection here means both are up.
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
# Schema changes run as the table owner (DB_MIGRATOR_*) and, like every maintenance role,
# straight against Postgres rather than the pooler (config/env.py): migrate, then the row-level
# security policies for new tables, then the drift check that gates the deploy. gunicorn itself
# keeps DB_ROLE=app (`app_user`, subject to RLS) — /api/ready refuses anything else.
if [ "${1:-}" = "gunicorn" ] && [ "${MIGRATE_ON_START:-true}" = "true" ]; then
  python manage.py ensure_bucket
  DB_ROLE=migrator python manage.py migrate --noinput
  DB_ROLE=migrator python manage.py rls_sync
  DB_ROLE=migrator python manage.py rls_sync --check
fi
exec "$@"
