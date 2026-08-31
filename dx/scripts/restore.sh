#!/usr/bin/env bash
# Load a dump from ./scripts/backup.sh into the current database (migrates first).
#   ./scripts/restore.sh dx-2026-08-28T12-00-00Z.json.gz
#   ./scripts/restore.sh --latest
# Existing rows with the same primary key are overwritten; nothing is deleted. Asks before
# it writes unless -y is given. Wrapper around `manage.py restore`. Runs as the table owner
# (migrate + RLS policies + loaddata across every tenant), like backup.sh.
set -euo pipefail
cd "$(dirname "$0")/../backend"
export DB_ROLE="${DB_ROLE:-migrator}"
exec uv run python manage.py restore "$@"
