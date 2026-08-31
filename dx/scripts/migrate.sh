#!/usr/bin/env bash
# Apply migrations and the row-level security policies as the table owner, then verify:
#   ./scripts/migrate.sh              # migrate + rls_sync + rls_sync --check
#   ./scripts/migrate.sh datasets     # extra args go to `manage.py migrate`
# The same three steps run in docker/entrypoint.sh on every deploy. Plain `manage.py migrate`
# fails on purpose: the default DATABASE_URL connects as `app_user`, which owns nothing.
set -euo pipefail
cd "$(dirname "$0")/../backend"
export DB_ROLE=migrator
uv run python manage.py migrate "$@"
uv run python manage.py rls_sync
uv run python manage.py rls_sync --check
