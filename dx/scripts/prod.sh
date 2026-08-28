#!/usr/bin/env bash
# Run the production stack (docker/docker-compose.prod.yml) with docker/.env.prod.
#   ./scripts/prod.sh               # up -d --wait (build the image first: ./scripts/build.sh)
#   ./scripts/prod.sh logs -f app   # any docker compose subcommand is passed through
#   ./scripts/prod.sh down
set -euo pipefail
cd "$(dirname "$0")/.."
env_file=docker/.env.prod
if [ ! -f "$env_file" ]; then
  echo "missing $env_file — copy docker/.env.prod.example and fill it in" >&2
  exit 1
fi
if [ $# -eq 0 ]; then
  set -- up -d --wait
fi
exec docker compose --env-file "$env_file" -f docker/docker-compose.prod.yml "$@"
