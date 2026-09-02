---
paths:
  - "**/backend/apps/core/backups.py"
  - "**/scripts/backup.sh"
  - "**/scripts/restore.sh"
  - "**/scripts/roundtrip.sh"
---

## Backups (`apps/core/backups.py`)

- A dump holds every user's rows: `backup`/`restore` need a connection RLS does not apply to
  (`./scripts/backup.sh`/`restore.sh` set `DB_ROLE=migrator`; the nightly task runs on the
  maintenance worker) and refuse the runtime role (`CrossTenantAccessRequired`) rather than
  silently writing a partial dump. `restore` also re-applies the RLS policies after `migrate`.
- `manage.py backup` (`./scripts/backup.sh`) dumps the database with `dumpdata` (natural keys;
  no content types/permissions/sessions/log entries) into `dx-<UTC timestamp>.json.gz` in the
  **`backups` storage** (`STORAGES["backups"]`): the `S3_BACKUP_BUCKET` bucket (`dx-backups`,
  versioned, created by `ensure_bucket`), or `backend/backups/` with `MEDIA_STORAGE=local`.
  `--list` shows the dumps, `--prune` keeps only the newest `BACKUP_KEEP` (30).
- `manage.py restore <name>|--latest [-y]` (`./scripts/restore.sh`) = `migrate` + `loaddata`:
  rows with the same pk are overwritten, nothing is deleted. A dump contains the event tables as
  well (`use_base_manager=True`, so soft-deleted rows are in it too), and the load runs inside
  `history.unversioned()` — otherwise the triggers would bump every restored version, duplicate
  its history, and then fail outright on an event row that already exists. `./scripts/roundtrip.sh` proves it
  against a fresh database (dev only, forces `MEDIA_STORAGE=local`).
- Nightly: `apps.core.tasks.backup_database` (`CELERY_BEAT_SCHEDULE`, 03:00 UTC, `WithRetry`)
  creates a dump and prunes; needs a running `beat`.
- Not in a dump: uploaded files (they live in the versioned media bucket — back up the store)
  and the Celery queue. For a production Postgres the provider's `pg_dump`/snapshots remain the
  primary backup; this is the app-level, restore-anywhere copy.
- Tests write to a throwaway directory (`settings_test.py`), never to a bucket:
  `apps/core/tests/test_backups.py`, command tests in `test_commands.py`.
