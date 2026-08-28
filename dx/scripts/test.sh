#!/usr/bin/env bash
# Run the backend tests (needs the dev Postgres: ./scripts/db.sh). Extra args go to pytest:
#   ./scripts/test.sh                       # everything
#   ./scripts/test.sh apps/datasets -k list # a subset
#   ./scripts/test.sh --reuse-db            # faster re-runs (add --create-db after migrations)
set -euo pipefail
cd "$(dirname "$0")/../backend"
exec uv run pytest "$@"
