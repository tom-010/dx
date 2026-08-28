#!/usr/bin/env bash
# Verify that backup + restore work end to end: dump, DESTROY the dev database
# (docker compose down -v), start a fresh one, restore the dump.
# Dev-only — it drops the local Postgres volume. Pass -y to skip the confirmation.
# Needs MEDIA_STORAGE=local (backend/backups/): `down -v` also wipes the object store, and
# with it any dump stored in the dx-backups bucket.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "${1:-}" != "-y" ]; then
  read -r -p "This deletes the local dev database volume and restores from a fresh dump. Continue? [y/N] " answer
  [ "$answer" = "y" ] || { echo "aborted"; exit 1; }
fi
export MEDIA_STORAGE=local
./scripts/backup.sh
./scripts/db.sh down -v
./scripts/db.sh
./scripts/restore.sh --latest -y
echo "roundtrip ok"
