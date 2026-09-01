#!/usr/bin/env bash
# Apply migrations + the RLS policies as the table owner (delegates to backend/scripts/migrate.sh;
# extra args go to `manage.py migrate`).
exec "$(dirname "$0")/../backend/scripts/migrate.sh" "$@"
