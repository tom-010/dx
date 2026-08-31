"""Typed environment configuration (pydantic-settings instead of raw os.environ)."""

from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import PostgresDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
# Docker/Compose/Swarm secrets: a file per key (`/run/secrets/SECRET_KEY` holds the value).
# Only used when the directory exists, so nothing changes on a dev machine.
SECRETS_DIR = Path("/run/secrets")
# Django's convention for generated dev keys; `Env` refuses it once DEBUG is off.
DEV_SECRET_KEY = "django-insecure-dev-only-change-me"
# Which database credentials a process uses, see `Env.DB_ROLE`.
DbRole = Literal["app", "migrator", "admin"]


class Env(BaseSettings):
    """All values can be overridden via environment variables, `backend/.env`, or files in
    `/run/secrets/<KEY>` (see SECRETS_DIR).

    Defaults are for development against docker/docker-compose.yml. Production (DEBUG=false)
    must set SECRET_KEY, ALLOWED_HOSTS and EMAIL_URL — `manage.py check --deploy` (run by
    docker/entrypoint.sh before serving) refuses to start otherwise; docker/.env.prod.example
    lists everything.
    """

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        secrets_dir=SECRETS_DIR if SECRETS_DIR.is_dir() else None,
        extra="ignore",
    )

    DEBUG: bool = True
    # Dev-only default, refused when DEBUG is off (`production_guards`): `openssl rand -hex 32`.
    SECRET_KEY: str = DEV_SECRET_KEY
    # Previous keys that sessions and signed links may still carry while SECRET_KEY is being
    # rotated. Remove them once everything signed with them has expired.
    SECRET_KEY_FALLBACKS: list[str] = []
    # Public host names, e.g. '["dx.example.com"]'. Required when DEBUG is off; with DEBUG and an
    # empty list Django accepts localhost by itself.
    ALLOWED_HOSTS: list[str] = []
    # Version label for logs and Sentry (docker/Dockerfile sets it to the git commit).
    APP_VERSION: str = "dev"

    # --- HTTPS. The app never terminates TLS itself; in production a proxy does (Caddy in
    # docker/docker-compose.prod.yml, or the platform's edge). HTTPS_ONLY turns on the redirect
    # to https, secure cookies, HSTS and trusting the proxy's X-Forwarded-Proto (settings.py).
    # Unset = the opposite of DEBUG; false only for plain-http smoke tests of the image.
    HTTPS_ONLY: bool | None = None
    # HSTS max-age (seconds) while HTTPS_ONLY. Start low; raise to 31536000 (a year, needed for
    # browser preload lists) once HTTPS is known to work everywhere — browsers remember it.
    SECURE_HSTS_SECONDS: int = 3600

    # --- Database. Matches the dev database in docker/docker-compose.yml. Query parameters
    # become psycopg connection options, e.g. `?sslmode=require` for a managed Postgres. The
    # credentials in the URL are the *runtime* role `app_user`, which row-level security applies
    # to (CLAUDE.md "Multitenancy"; docker/postgres/10-roles.sh creates the roles).
    DATABASE_URL: PostgresDsn = PostgresDsn("postgres://app_user:app_user@localhost:5432/dx")
    # Which credentials this process connects with (host/port/database always come from
    # DATABASE_URL): "app" = the URL's own (RLS enforced), "migrator" = DB_MIGRATOR_* (owns the
    # tables: migrate, rls_sync, backups, the test suite), "admin" = DB_ADMIN_* (BYPASSRLS:
    # `manage.py shell_admin`). The web process and the workers always run as "app".
    # The Django admin is a staff UI over tenant data (apps/core/admin.py), not part of the
    # product: mounted with DEBUG, absent otherwise unless this says so explicitly. Off also
    # removes the interactive API docs, which need an admin session to log in with.
    ADMIN_ENABLED: bool | None = None
    DB_ROLE: DbRole = "app"
    # Dev default: the compose superuser `dx`. Production: app_migrator (docker/.env.prod.example).
    DB_MIGRATOR_USER: str | None = "dx"
    DB_MIGRATOR_PASSWORD: str | None = "dx"
    # No default on purpose — these must not exist in the web/worker environment. Dev: put
    # DB_ADMIN_USER=app_admin / DB_ADMIN_PASSWORD=app_admin into backend/.env.
    DB_ADMIN_USER: str | None = None
    DB_ADMIN_PASSWORD: str | None = None
    # Keep connections open between requests for this many seconds (0 = one per request; use 0
    # behind a transaction-pooling pgbouncer).
    DB_CONN_MAX_AGE: int = 60
    # Django's cache (sessions, ...): Valkey/Redis, database 1 (Celery uses 0).
    CACHE_URL: str = "redis://localhost:6379/1"
    # Location of the Vite build output (`pnpm build`); collected by collectstatic.
    FRONTEND_DIST: Path = BASE_DIR.parent / "frontend" / "dist"

    # --- Logging (config/logging.py). `console` = plain readable lines for developers (the
    # default with DEBUG), `json` = structured one-object-per-line logs for production (the
    # default without DEBUG, i.e. the docker image). LOG_FORMAT unset = follow DEBUG.
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    LOG_FORMAT: Literal["console", "json"] | None = None
    # Log every SQL query (`django.db.backends` at DEBUG); needs DEBUG=true to have any effect.
    LOG_SQL: bool = False

    # --- Email. Unset = print to the console (development). Production needs a real mailer,
    # `smtp://user:password@host:587?tls=true` (or `smtps://…:465`), or the explicit `dummy://`
    # for a deployment that sends no email at all. `django_mailer()` below does the translation.
    EMAIL_URL: str | None = None
    DEFAULT_FROM_EMAIL: str = "noreply@localhost"

    # --- Error monitoring (https://sentry.io, NOTES.md §8). Unset = off.
    SENTRY_DSN: str | None = None
    # Share of requests/tasks traced for performance monitoring (0 = report errors only).
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0

    # --- Auth (apps.accounts). A login yields two JWTs: a short-lived access token that every
    # request carries, and a refresh token that renews it (POST /api/auth/refresh, rotated on
    # every use, revoked by logout). Keep the access lifetime short — it cannot be revoked.
    ACCESS_TOKEN_LIFETIME_MINUTES: int = 15
    # How long a login stays valid without use; a refresh extends it by this again.
    REFRESH_TOKEN_LIFETIME_DAYS: int = 30
    # Optional static bearer token for CI/scripts; authenticates as the first superuser.
    API_FIXED_TOKEN: str | None = None
    # Allow self-service sign-up via POST /api/auth/register.
    REGISTRATION_OPEN: bool = False

    # --- File storage (Django's default storage; apps.documents). "s3" = the S3-compatible object
    # store (RustFS from docker/docker-compose.yml in dev, R2/S3 in prod — NOTES.md §2); "local" =
    # plain disk under backend/media (tests, or a machine without Docker).
    MEDIA_STORAGE: Literal["s3", "local"] = "s3"
    # None = the provider's default endpoint (real AWS S3). Dev default: the compose `s3` service.
    S3_ENDPOINT_URL: str | None = "http://localhost:9100"
    S3_ACCESS_KEY: str = "dx"
    S3_SECRET_KEY: str = "dxdxdxdx"
    # Created (with versioning enabled) by `manage.py ensure_bucket`.
    S3_BUCKET: str = "dx-media"
    S3_REGION: str = "us-east-1"

    # --- Database backups (apps/core/backups.py, `manage.py backup`). Dumps go to their own
    # bucket (same store/credentials as S3_BUCKET) or to backend/backups with MEDIA_STORAGE=local.
    S3_BACKUP_BUCKET: str = "dx-backups"
    # How many dumps the nightly task keeps; older ones are deleted after each successful backup.
    BACKUP_KEEP: int = 30

    # --- Cross-origin. Empty for the web build (same origin); the Capacitor apps add their
    # origins here (NOTES.md §8). JSON lists, e.g. '["capacitor://localhost"]'.
    CORS_ALLOWED_ORIGINS: list[str] = []
    CSRF_TRUSTED_ORIGINS: list[str] = []

    # --- Celery. Redis (Valkey) from docker/docker-compose.yml is broker and result store.
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    # Defaults to the broker URL.
    CELERY_RESULT_BACKEND: str | None = None
    # Run tasks inline in the calling process instead of a worker. Off by default so dev behaves
    # like production (start ./scripts/celery.sh); tests force it on (settings_test.py).
    CELERY_EAGER: bool = False

    @property
    def https_only(self) -> bool:
        return self.HTTPS_ONLY if self.HTTPS_ONLY is not None else not self.DEBUG

    def database_credentials(self, role: DbRole | None = None) -> tuple[str, str] | None:
        """(user, password) for `role` (default DB_ROLE); None = the DATABASE_URL's own.

        Raises when a role is requested whose credentials are not configured, so a deploy step
        started with the wrong environment fails before it touches the database.
        """
        role = role or self.DB_ROLE
        if role == "app":
            return None
        user, password = {
            "migrator": (self.DB_MIGRATOR_USER, self.DB_MIGRATOR_PASSWORD),
            "admin": (self.DB_ADMIN_USER, self.DB_ADMIN_PASSWORD),
        }[role]
        if not user or password is None:
            raise ValueError(
                f"DB_ROLE={role} needs DB_{role.upper()}_USER and DB_{role.upper()}_PASSWORD"
            )
        return user, password

    @property
    def admin_enabled(self) -> bool:
        """Whether `/admin/` is mounted at all (default: only with DEBUG)."""
        return self.ADMIN_ENABLED if self.ADMIN_ENABLED is not None else self.DEBUG

    def audit_credentials(self) -> tuple[str, str] | None:
        """(user, password) for the admin's cross-tenant alias, or None when it is not set up.

        Same role as `manage.py shell_admin` (`DB_ADMIN_*`, BYPASSRLS). Unset — the production
        default — is a supported state, not an error: `apps/core/admin.py` then shows a superuser
        their own tenant like any other staff user. Deploying the credential is what turns
        cross-tenant visibility on, so the decision stays with the environment.
        """
        if not self.DB_ADMIN_USER or self.DB_ADMIN_PASSWORD is None:
            return None
        return self.database_credentials("admin")

    @model_validator(mode="after")
    def production_guards(self) -> Self:
        """Refuse to start with a known secret rather than serve with it."""
        if not self.DEBUG and self.SECRET_KEY == DEV_SECRET_KEY:
            raise ValueError(
                "SECRET_KEY is the development default; set a random value "
                "(openssl rand -hex 32) when DEBUG=false"
            )
        return self


def django_database(
    url: PostgresDsn, *, conn_max_age: int = 0, credentials: tuple[str, str] | None = None
) -> dict[str, Any]:
    """Translate a Postgres DSN into Django's DATABASES entry format.

    Query parameters (`?sslmode=require&connect_timeout=5`) are handed to psycopg as OPTIONS.
    `credentials` replaces the user/password of the URL (`Env.database_credentials()`).
    """
    host = url.hosts()[0]
    # pydantic keeps credentials percent-encoded.
    user, password = credentials or (
        unquote(host["username"] or ""),
        unquote(host["password"] or ""),
    )
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": (url.path or "/").lstrip("/"),
        "USER": user,
        "PASSWORD": password,
        "HOST": host["host"] or "",
        "PORT": host["port"] or "",
        "OPTIONS": dict(url.query_params()),
        "CONN_MAX_AGE": conn_max_age,
        # Verify a reused connection before each request: survives database restarts/failovers.
        "CONN_HEALTH_CHECKS": True,
        # The tenant middleware owns the request transaction (`SET LOCAL` has to run inside it,
        # before the view); Django's per-view transactions would start too late. Keep off.
        "ATOMIC_REQUESTS": False,
    }


_SMTP_DEFAULT_PORT = {"smtp": 25, "smtps": 465}


def django_mailer(url: str | None) -> dict[str, Any]:
    """Translate EMAIL_URL into a MAILERS entry (console output when unset).

    `smtp://user:password@host:587?tls=true` (STARTTLS), `smtps://…:465` (implicit TLS),
    `console://`, `dummy://` (discard — only for deployments that send no email).
    """
    if not url:
        return {"BACKEND": "django.core.mail.backends.console.EmailBackend"}
    parts = urlsplit(url)
    if parts.scheme in ("console", "dummy", "locmem"):
        return {"BACKEND": f"django.core.mail.backends.{parts.scheme}.EmailBackend"}
    if parts.scheme not in _SMTP_DEFAULT_PORT:
        raise ValueError(f"EMAIL_URL: unsupported scheme {parts.scheme!r} (smtp, smtps, dummy)")
    query = dict(parse_qsl(parts.query))
    use_tls = query.get("tls", "").lower() in ("1", "true", "yes")
    return {
        "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
        "OPTIONS": {
            "host": parts.hostname or "localhost",
            "port": parts.port or (587 if use_tls else _SMTP_DEFAULT_PORT[parts.scheme]),
            "username": unquote(parts.username or ""),
            "password": unquote(parts.password or ""),
            "use_tls": use_tls,
            "use_ssl": parts.scheme == "smtps",
            # A stuck mail server must not hang a request thread.
            "timeout": 10,
        },
    }


env = Env()
