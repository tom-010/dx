#!/usr/bin/env bash
# Manage the dev infrastructure containers (Postgres + Redis + S3). No args = start in background
# and create the media bucket (manage.py ensure_bucket).
#   ./scripts/db.sh            # up -d --wait + ensure_bucket
#   ./scripts/db.sh down       # stop
#   ./scripts/db.sh logs -f    # any docker compose subcommand
set -euo pipefail
cd "$(dirname "$0")/.."
if [ $# -eq 0 ]; then
  docker compose -f docker/docker-compose.yml up -d --wait
  exec uv --directory backend run python manage.py ensure_bucket
fi
exec docker compose -f docker/docker-compose.yml "$@"
