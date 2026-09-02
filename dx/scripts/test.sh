#!/usr/bin/env bash
# Run the backend tests (needs the dev Postgres: ./scripts/db.sh). Extra args go to pytest:
#   ./scripts/test.sh                       # everything, in parallel
#   ./scripts/test.sh apps/datasets -k list # a subset
#   ./scripts/test.sh --reuse-db            # faster re-runs (add --create-db after migrations)
#   PYTEST_WORKERS=0 ./scripts/test.sh      # serial, for pdb/-s or a confusing failure
#
# Parallel by default (pytest-xdist). Eight workers rather than one per core: the tests wait on
# Postgres more than they compute, so more workers stop paying off around there — measured, on a
# 24-core machine, at ~10s either way for 2k tests. Each worker gets its own test database, so
# the first run of a given width pays to create them.
set -euo pipefail
cd "$(dirname "$0")/../backend"

workers="${PYTEST_WORKERS:-$(( $(nproc 2>/dev/null || echo 4) < 8 ? $(nproc 2>/dev/null || echo 4) : 8 ))}"
if [[ "$workers" == "0" ]]; then
  exec uv run pytest "$@"
fi
exec uv run pytest -n "$workers" "$@"
