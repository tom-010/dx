#!/usr/bin/env bash
# Manage the dev infrastructure containers (Postgres + Redis + S3). No args = start in background,
# make sure the database roles exist (docker/postgres/10-roles.sh, idempotent — also for volumes
# created before it) and create the media bucket (manage.py ensure_bucket).
#   ./scripts/db.sh            # up -d --wait + roles + ensure_bucket
#   ./scripts/db.sh down       # stop
#   ./scripts/db.sh logs -f    # any docker compose subcommand
# Migrations are a separate step: ./scripts/migrate.sh (migrate + RLS policies).
set -euo pipefail
cd "$(dirname "$0")/.."
if [ $# -eq 0 ]; then
  docker compose -f docker/docker-compose.yml up -d --wait
  docker compose -f docker/docker-compose.yml exec -T db sh /docker-entrypoint-initdb.d/10-roles.sh
  exec uv --directory backend run python manage.py ensure_bucket
fi
exec docker compose -f docker/docker-compose.yml "$@"
