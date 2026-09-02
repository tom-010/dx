#!/usr/bin/env bash
# Everything CI would run — use it before finishing a change (see CLAUDE.md):
# lint.sh, backend tests, the model examples, frontend build (tsc + vite), and the
# generated-client drift check.
#
# Optimised for the loop you run it in, not for CI: the tests run in parallel and **reuse the
# test databases** rather than rebuilding them. That is only safe while the schema has not
# moved, so the migration files are fingerprinted below and the databases are rebuilt when it
# has. CI has the time to start from nothing (`ci.py`).
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
. scripts/_pnpm.sh
./scripts/lint.sh

# `--reuse-db` against a stale schema fails in ways that read like real bugs, so decide it here
# instead of leaving it to whoever last ran a migration: hash every migration file, keep the
# hash beside the test databases it describes, and rebuild when the two disagree.
stamp="backend/.pytest-db-stamp"
migrations=$(find backend/apps -path "*/migrations/*.py" -type f -print0 | sort -z | xargs -0 cat | shasum | cut -d" " -f1)
if [[ -f "$stamp" && "$(cat "$stamp")" == "$migrations" ]]; then
  db_flag="--reuse-db"
else
  db_flag="--create-db"
  echo "== backend: migrations changed, rebuilding the test databases"
fi
echo "== backend: pytest";     "$(dirname "$0")/test.sh" -q "$db_flag"
echo "$migrations" > "$stamp"
# Every model hands out one saveable example of itself (apps/core/examples.py): built and
# written here against the development database, each tree in a savepoint that is rolled back.
echo "== backend: examples";   (cd backend && uv run python manage.py check_examples)
echo "== frontend: build";     (cd frontend && pnpm build)
echo "== sync_schema: --check"; ./scripts/sync_schema.sh --check
echo "all checks passed"
