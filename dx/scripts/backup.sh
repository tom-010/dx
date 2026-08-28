#!/usr/bin/env bash
# Dump the database to the backup storage: the `dx-backups` bucket of the object store, or
# backend/backups/ with MEDIA_STORAGE=local (see CLAUDE.md "Backups"). Wrapper around
# `manage.py backup`; extra args are passed through (`--list`, `--prune`).
# Uploaded files are not part of a dump — they live in the (versioned) media bucket.
set -euo pipefail
cd "$(dirname "$0")/../backend"
exec uv run python manage.py backup "$@"
