#!/usr/bin/env bash
# End-to-end type pipeline: Django/ninja → openschema.json → orval → frontend/src/api/.
#   ./scripts/sync_schema.sh          # regenerate spec + client
#   ./scripts/sync_schema.sh --check  # fail if the committed spec/client are out of date (CI)
#   ./scripts/sync_schema.sh --watch  # regenerate whenever backend code changes
#
# Needs NO running Django server and NO database: `export_openapi_schema` builds the spec
# in-process from the NinjaAPI object (config.api.api). Only uv (Python) and pnpm (orval) are used.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)

# shellcheck disable=SC1091
. scripts/_pnpm.sh

generate() {
  (cd backend && uv run python manage.py export_openapi_schema \
      --api config.api.api --output "$ROOT/openschema.json" --indent 2 --sorted)
  (cd frontend && pnpm exec orval --config ../orval.config.ts)
}

case "${1:-}" in
  --check)
    snapshot=$(mktemp -d)
    cp openschema.json "$snapshot/openschema.json"
    cp -r frontend/src/api "$snapshot/api"
    generate >/dev/null
    if diff -q openschema.json "$snapshot/openschema.json" >/dev/null \
       && diff -rq frontend/src/api "$snapshot/api" >/dev/null; then
      echo "sync_schema: up to date"
    else
      echo "sync_schema: openschema.json / frontend/src/api are out of date (regenerated in place; commit them)" >&2
      diff -r "$snapshot/api" frontend/src/api || true
      exit 1
    fi
    ;;
  --watch)
    cd backend && exec uv run watchfiles --filter python "$ROOT/scripts/sync_schema.sh" apps config
    ;;
  "")
    generate
    ;;
  *)
    echo "usage: $0 [--check|--watch]" >&2
    exit 2
    ;;
esac
