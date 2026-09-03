#!/bin/sh
# Application roles for row-level security (CLAUDE.md "Multitenancy", backend/apps/core/rls.py).
# Idempotent: the Postgres image runs it once on a fresh volume (docker-entrypoint-initdb.d).
# ./scripts/db.sh re-runs it on every start, so a dev volume created before this script catches
# up by itself. In production nothing re-runs it — on an existing volume (a stack that predates
# this change) run it once by hand, or every container fails with "password authentication
# failed for user app_user":
#   ./scripts/prod.sh exec -T db sh /docker-entrypoint-initdb.d/10-roles.sh
#
#   app_migrator  owns every table; runs migrate + rls_sync (DB_ROLE=migrator). Not a superuser.
#   app_user      the web process and Celery workers; RLS applies to it (never BYPASSRLS, never an
#                 owner). DATABASE_URL carries its credentials.
#   app_admin     BYPASSRLS for `manage.py shell_admin` and support tooling; credentials only in
#                 ops shells, never in the app/worker environment.
#   pgbouncer     the connection pooler's lookup role (the `pgbouncer` service): may call
#                 pgbouncer.user_lookup() to fetch a role's password secret for its own SCRAM
#                 check, and nothing else — no table, not even the public schema.
#
# Passwords come from the container environment (APP_MIGRATOR_PASSWORD, APP_USER_PASSWORD,
# APP_ADMIN_PASSWORD, PGBOUNCER_PASSWORD; the dev compose passes the role names). An empty/unset variable leaves that
# role's password untouched — a fresh app_admin then has none and cannot log in until an operator
# sets one (`ALTER ROLE app_admin PASSWORD '…'`), which keeps it out of the app's environment.
# The bootstrap superuser (POSTGRES_USER, `dx`) keeps working and is the dev migrator: tables it
# created earlier are handed to app_migrator below, so a dev volume from before this script ends
# up with the same ownership as a fresh one.
set -eu

password_sql() {  # password_sql ROLE VALUE — an ALTER ROLE statement, or nothing for an empty value
  if [ -n "$2" ]; then
    printf "ALTER ROLE %s PASSWORD '%s';\n" "$1" "$(printf '%s' "$2" | sed "s/'/''/g")"
  fi
}

{
cat <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_migrator') THEN
    CREATE ROLE app_migrator LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    CREATE ROLE app_user LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_admin') THEN
    CREATE ROLE app_admin LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgbouncer') THEN
    CREATE ROLE pgbouncer LOGIN;
  END IF;
END
$$;

-- Attributes are (re)asserted every run: app_user must never bypass RLS or become an owner.
ALTER ROLE app_migrator LOGIN NOSUPERUSER NOBYPASSRLS;
ALTER ROLE app_user LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
ALTER ROLE app_admin LOGIN NOSUPERUSER BYPASSRLS;
ALTER ROLE pgbouncer LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
SQL
password_sql app_migrator "${APP_MIGRATOR_PASSWORD:-}"
password_sql app_user "${APP_USER_PASSWORD:-}"
password_sql app_admin "${APP_ADMIN_PASSWORD:-}"
password_sql pgbouncer "${PGBOUNCER_PASSWORD:-}"
cat <<'SQL'
-- PgBouncer's auth_query (docker/docker-compose*.yml): the SCRAM secret of the role that is
-- logging in, read from pg_shadow on the pooler's behalf (SECURITY DEFINER: the function runs
-- as the superuser that created it). Superusers and BYPASSRLS roles are never returned, so
-- the pooled endpoint only hands out connections row-level security applies to; those roles
-- connect to Postgres directly. Column names are what PgBouncer expects: user, then secret.
CREATE SCHEMA IF NOT EXISTS pgbouncer;
CREATE OR REPLACE FUNCTION pgbouncer.user_lookup(username text, OUT uname text, OUT phash text)
  RETURNS record
  LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog
  AS $$
    SELECT usename::text, passwd FROM pg_shadow
    WHERE usename = username AND NOT usesuper AND NOT usebypassrls;
  $$;
REVOKE ALL ON SCHEMA pgbouncer FROM PUBLIC;
REVOKE ALL ON FUNCTION pgbouncer.user_lookup(text) FROM PUBLIC;
GRANT USAGE ON SCHEMA pgbouncer TO pgbouncer;
GRANT EXECUTE ON FUNCTION pgbouncer.user_lookup(text) TO pgbouncer;

-- The migrator may SET ROLE app_user: the test suite verifies the policies from the runtime role.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_auth_members m
    JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles g ON g.oid = m.member
    WHERE r.rolname = 'app_user' AND g.rolname = 'app_migrator'
  ) THEN
    GRANT app_user TO app_migrator;
  END IF;
END
$$;

ALTER DATABASE "__DBNAME__" OWNER TO app_migrator;
ALTER SCHEMA public OWNER TO app_migrator;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO app_user, app_admin;

-- Tables created by the bootstrap superuser (dev volumes from before this script) → migrator.
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tableowner = current_user
  LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO app_migrator', r.tablename);
  END LOOP;
END
$$;

-- Future tables/sequences the migrator creates are readable and writable by the app roles
-- (`manage.py rls_sync` grants the same explicitly after every migrate, so this is belt and braces).
ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user, app_admin;
ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO app_user, app_admin;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user, app_admin;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user, app_admin;
SQL
} | sed "s/__DBNAME__/$POSTGRES_DB/" | psql -v ON_ERROR_STOP=1 --quiet --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"
echo "roles app_migrator / app_user / app_admin / pgbouncer ready"
