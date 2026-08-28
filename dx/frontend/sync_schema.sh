#!/usr/bin/env bash
# Frontend-side entry point: regenerates openschema.json and src/api/ from the Django backend.
# Same options as the root script: --check, --watch.
set -euo pipefail
cd "$(dirname "$0")/.."
exec ./scripts/sync_schema.sh "$@"
