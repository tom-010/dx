#!/usr/bin/env bash
# Dump the database to the backup storage: the `dx-backups` bucket of the object store, or
# backend/backups/ with MEDIA_STORAGE=local (see CLAUDE.md "Backups"). Wrapper around
# `manage.py backup`; extra args are passed through (`--list`, `--prune`).
# Uploaded files are not part of a dump — they live in the (versioned) media bucket.
# A dump holds every user's rows, so it connects as the table owner (row-level security does
# not apply to it; as `app_user` the command refuses to run). DB_ROLE=admin works as well.
set -euo pipefail
cd "$(dirname "$0")/../backend"
export DB_ROLE="${DB_ROLE:-migrator}"
exec uv run python manage.py backup "$@"
