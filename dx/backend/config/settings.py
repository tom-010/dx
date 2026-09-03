"""
Django settings for the dx backend.

Environment-dependent values come from `config.env` (pydantic-settings).
https://docs.djangoproject.com/en/6.1/ref/settings/
"""

from typing import Any

import django_stubs_ext
from celery.schedules import crontab

from config.env import BASE_DIR, django_database, django_mailer, env
from config.logging import configure_structlog, logging_config
from config.static import vite_immutable_file

# Makes generic Django classes (QuerySet, ModelAdmin, ...) subscriptable at runtime.
django_stubs_ext.monkeypatch()

SECRET_KEY = env.SECRET_KEY
SECRET_KEY_FALLBACKS = env.SECRET_KEY_FALLBACKS
DEBUG = env.DEBUG
# The build this process is running: the short commit `scripts/build.sh` stamps into the image,
# "dev" outside one. Sentry reports it as the release, and `Lineage.release` records it beside
# the stack that wrote an edge, so a file and a line can be resolved back to the code that ran.
APP_VERSION = env.APP_VERSION
# `/admin/` is only mounted when this is on (config/urls.py). The admin shows tenant data, so
# production does not serve it: administer through `manage.py shell_as` / `shell_admin`.
ADMIN_ENABLED = env.admin_enabled
# The lineage explorer (apps/core/explorer.py), same deal: a read-only browser over every table,
# mounted only in development. Derived here rather than read from `settings.DEBUG` inside
# `config/urls.py`, because Django's test runner sets DEBUG to False *after* settings are
# imported — a URLconf gated on it directly would silently vanish for the whole test suite.
EXPLORER_ENABLED = DEBUG
# The development front door at `/` (config/home.py): links to the pages above, in place of the
# SPA catch-all, which in development answers "frontend not built" (the SPA runs on Vite). Read
# from a setting rather than from `settings.DEBUG` in `config/urls.py` for the same reason as
# EXPLORER_ENABLED above.
DEV_HOME_ENABLED = DEBUG
# Container health checks (Docker HEALTHCHECK, compose) reach the app via loopback; those names
# are always allowed next to the public ones — they cannot be used for host-header poisoning.
# While DEBUG with an empty list, Django allows them by itself.
ALLOWED_HOSTS = [*env.ALLOWED_HOSTS, "localhost", "127.0.0.1", "[::1]"] if env.ALLOWED_HOSTS else []


# HTTPS and security headers — https://docs.djangoproject.com/en/6.1/howto/deployment/checklist/
# Production runs behind a TLS-terminating proxy (Caddy in docker/docker-compose.prod.yml, or the
# platform's edge); gunicorn never speaks TLS. HTTPS_ONLY (default: not DEBUG) switches all of
# this on at once; `manage.py check --deploy` verifies it (docker/entrypoint.sh runs it).

HTTPS_ONLY = env.https_only
SECURE_SSL_REDIRECT = HTTPS_ONLY
# The container health checks talk plain http to the app itself, without the proxy.
SECURE_REDIRECT_EXEMPT = [r"^api/(health|ready)$"]
# Trust the proxy's X-Forwarded-Proto so request.is_secure() is right (no redirect loop, https in
# absolute URLs). Safe only because port 8000 is never reachable except through the proxy, which
# overwrites the header (Caddy, Cloudflare, Fly and Railway all do).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if HTTPS_ONLY else None
SESSION_COOKIE_SECURE = HTTPS_ONLY
CSRF_COOKIE_SECURE = HTTPS_ONLY
# HSTS: browsers remember to use https for SECURE_HSTS_SECONDS (config/env.py on raising it).
SECURE_HSTS_SECONDS = env.SECURE_HSTS_SECONDS if HTTPS_ONLY else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = HTTPS_ONLY
SECURE_HSTS_PRELOAD = HTTPS_ONLY
# Django's defaults already fit a SPA + API: SECURE_CONTENT_TYPE_NOSNIFF, X_FRAME_OPTIONS=DENY,
# SECURE_REFERRER_POLICY=same-origin, SECURE_CROSS_ORIGIN_OPENER_POLICY=same-origin.


# Application definition

INSTALLED_APPS = [
    # Above django.contrib.admin on purpose: its template overrides add the "Events" buttons to
    # every tracked model's admin pages, and the first app that defines a template path wins.
    "pghistory.admin",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",  # GinIndex + SearchVector: snapshot full-text search (documents)
    "ninja",  # management commands (export_openapi_schema)
    "corsheaders",
    "django_structlog",
    "django_scopes",  # ORM tenant scope (apps/core/models.py::OwnedManager)
    "pgtrigger",  # database triggers as model Meta (apps/core/models.py::VersionedModel)
    "pghistory",  # trigger-captured version history (apps/core/history.py)
    # Feature apps live under apps/<name>/
    "apps.core",
    "apps.accounts",
    "apps.datasets",
    "apps.documents",
    "apps.gallery",
    "apps.notes",
    "apps.notifications",
    "apps.timeline",
    # needle: installed-apps (manage.py newapp inserts new apps above this line)
]

AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Before anything that can answer a request (WhiteNoise, CommonMiddleware).
    "corsheaders.middleware.CorsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Groups everything one request writes under a single pghistory context id, so the revision
    # page can show "one save" instead of a row per table. Before TenantMiddleware on purpose:
    # it swaps in a request class that re-binds the context whenever `request.user` is assigned,
    # which is what TenantMiddleware does once it has verified the bearer token.
    "apps.core.history.HistoryMiddleware",
    # Verifies the bearer token once and runs the rest of the request inside a transaction with
    # the tenant context set (`SET LOCAL app.user_id` + ORM scope) — after AuthenticationMiddleware
    # (which would overwrite request.user), before anything that reads owned data.
    "apps.core.middleware.TenantMiddleware",
    # The same context for `/admin/`, resolved from the admin session instead of a bearer token.
    # Without it every admin page over owned data raises: the ORM scope is missing and the
    # policies fail closed.
    "apps.core.middleware.AdminTenantMiddleware",
    # Binds request_id/user_id/ip to every log line of a request (config/logging.py).
    "django_structlog.middlewares.RequestMiddleware",
]

ROOT_URLCONF = "config.urls"

# Only the Capacitor apps are cross-origin (the web build is same-origin behind WhiteNoise).
CORS_ALLOWED_ORIGINS = env.CORS_ALLOWED_ORIGINS
CSRF_TRUSTED_ORIGINS = env.CSRF_TRUSTED_ORIGINS

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database
# https://docs.djangoproject.com/en/6.1/ref/settings/#databases

# DB_ROLE picks the credentials: the runtime role `app_user` by default (row-level security
# applies), the table owner for migrations/backups, BYPASSRLS for shell_admin. DB_POOLED picks
# the endpoint: PgBouncer for the web process, Postgres itself for everything else (config/env.py).
DATABASES = {
    "default": django_database(
        env.database_url(),
        conn_max_age=env.DB_CONN_MAX_AGE,
        credentials=env.database_credentials(),
        pooled=env.pooled(),
    )
}


# Multitenancy (tenant == user; CLAUDE.md "Multitenancy"). Two enforcement layers ride on
# `apps.core.models.OwnedModel`: the ORM scope (`OwnedManager`, django-scopes state) and
# Postgres row-level security (apps/core/rls.py, `manage.py rls_sync`). The request context
# comes from `apps.core.middleware.TenantMiddleware`, tasks use `apps.core.tasks.tenant_task`.

# The infrastructure apps hold shared tables (users, tokens, sessions); every other app under
# apps/ is a tenant app: all of its concrete models must inherit OwnedModel (apps/core/checks.py,
# tenant.E001). A new app is a tenant app by default — nothing to register.
SHARED_APPS = ["apps.core", "apps.accounts"]
TENANT_APPS = [app for app in INSTALLED_APPS if app.startswith("apps.") and app not in SHARED_APPS]
# Models inside tenant apps that are deliberately unowned ("app_label.Model"). Keep this empty;
# add an entry only after a security review.
SHARED_MODELS: list[str] = []
# The Postgres session variable the RLS policies compare `owner_id` against (apps/core/db.py).
TENANT_GUC = "app.user_id"


# Version history (django-pghistory; CLAUDE.md "Versioning and lineage"). Every write to a
# tracked table is mirrored into its event table by a Postgres trigger, so bulk writes, raw SQL
# and data migrations cannot produce a current state with no version row behind it.

# Event tables are append-only: pgtrigger rejects UPDATE and DELETE on them. Lineage edges point
# at event rows, and a lineage graph whose nodes can be edited or vanish is not a lineage graph.
PGHISTORY_APPEND_ONLY = True
# `pghistory_context` is a single shared table with no owner column: every tenant can read every
# row (its upsert function needs SELECT and UPDATE on it). Nothing tenant-identifying may go into
# the metadata — no user id, no resolved URL with object ids in it. See apps/core/history.py.
PGHISTORY_MIDDLEWARE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
# Every event table is generated from this base, so they all share one column set — which is
# what lets the aggregate `Events` page union them (apps/core/history.py::Event). Set here
# rather than passed per model: a table generated with a different base would union badly.
PGHISTORY_BASE_MODEL = "apps.core.history.Event"

# The admin's aggregate events page (`pghistory.admin`, CLAUDE.md "Admin").
# It unions *every* event table, so its cost grows with the number of tracked models and we
# track all of them: show nothing until the page is filtered or reached from another admin page.
# It needs no tenant column of its own: every event table carries `owner_id` with the
# `tenant_isolation` policy, so without a context the page returns nothing, not everything.
PGHISTORY_ADMIN_ALL_EVENTS = False
# `version` is the authoritative order of the version chain, but the aggregate model cannot
# carry it as a column (pghistory only lets a proxy field read `pgh_context`), so the page
# orders by write time and shows the version inside `pgh_diff`.
PGHISTORY_ADMIN_ORDERING = ["-pgh_created_at"]
PGHISTORY_ADMIN_LIST_DISPLAY = ["pgh_created_at", "pgh_obj_model", "pgh_obj_id", "pgh_diff"]


# Cache — Valkey/Redis, shared by every gunicorn and Celery process (settings_test.py: in-memory)
# https://docs.djangoproject.com/en/6.1/topics/cache/

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env.CACHE_URL,
        "KEY_PREFIX": "dx",
        # Fail fast when the store is unreachable instead of hanging a request thread.
        "OPTIONS": {"socket_connect_timeout": 2, "socket_timeout": 2},
    }
}
# Sessions (admin, API docs): served from the cache, written through to the database — a cache
# flush or restart logs nobody out. The API itself is stateless (bearer tokens).
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"


# Logging (config/logging.py): plain readable lines in dev, structured JSON lines in prod.

LOG_FORMAT = env.LOG_FORMAT or ("console" if DEBUG else "json")
LOGGING = logging_config(level=env.LOG_LEVEL, fmt=LOG_FORMAT, sql=env.LOG_SQL and DEBUG)
configure_structlog()
DJANGO_STRUCTLOG_CELERY_ENABLED = True


# API (django-ninja): list endpoints paginate with `?page=&page_size=` (ninja.pagination).

NINJA_PAGINATION_PER_PAGE = 50
NINJA_PAGINATION_MAX_PER_PAGE_SIZE = 500


# Password validation
# https://docs.djangoproject.com/en/6.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Internationalization
# https://docs.djangoproject.com/en/6.1/topics/i18n/

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# Static files (CSS, JavaScript, Images) — served by WhiteNoise
# https://docs.djangoproject.com/en/6.1/howto/static-files/
# https://whitenoise.readthedocs.io/

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# The Vite build output (built with base="/static/") is collected as static files.
FRONTEND_DIST = env.FRONTEND_DIST
STATICFILES_DIRS = [FRONTEND_DIST] if FRONTEND_DIST.is_dir() else []
# index.html of the SPA, served by config.spa for all non-API routes (after collectstatic).
SPA_INDEX = STATIC_ROOT / "index.html"

# Media files (uploads): stored in the S3-compatible object store by default (local disk with
# MEDIA_STORAGE=local), served by Django at MEDIA_URL via signed, expiring links — see
# config/media.py. Keys are `upload_to` + file name; on a name clash django-storages appends a
# random suffix (file_overwrite=False) so two files never share (or overwrite) one key.
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"  # answered by config.media.serve_media (config/urls.py)
# How long a `FileField.url` link stays valid.
MEDIA_LINK_MAX_AGE = 60 * 60  # seconds

S3_STORAGE: dict[str, Any] = {
    "BACKEND": "config.media.S3MediaStorage",
    "OPTIONS": {
        "endpoint_url": env.S3_ENDPOINT_URL,
        "access_key": env.S3_ACCESS_KEY,
        "secret_key": env.S3_SECRET_KEY,
        "bucket_name": env.S3_BUCKET,
        "region_name": env.S3_REGION,
        # RustFS/MinIO resolve buckets by path (`/bucket/key`), not by subdomain.
        "addressing_style": "path",
        "signature_version": "s3v4",
        "file_overwrite": False,
        "default_acl": None,
    },
}
LOCAL_STORAGE: dict[str, Any] = {"BACKEND": "config.media.LocalMediaStorage"}

# Database dumps (apps/core/backups.py): own bucket in the same store, or backend/backups on disk.
S3_BACKUP_STORAGE: dict[str, Any] = {
    "BACKEND": "storages.backends.s3.S3Storage",
    "OPTIONS": {**S3_STORAGE["OPTIONS"], "bucket_name": env.S3_BACKUP_BUCKET},
}
LOCAL_BACKUP_STORAGE: dict[str, Any] = {
    "BACKEND": "django.core.files.storage.FileSystemStorage",
    "OPTIONS": {"location": BASE_DIR / "backups"},
}
BACKUP_KEEP = env.BACKUP_KEEP

STORAGES = {
    "default": S3_STORAGE if env.MEDIA_STORAGE == "s3" else LOCAL_STORAGE,
    "backups": S3_BACKUP_STORAGE if env.MEDIA_STORAGE == "s3" else LOCAL_BACKUP_STORAGE,
    # Compressed (gzip + brotli) but not manifest-hashed: Vite already hashes its assets.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
# Vite emits `name-<8 char hash>.ext`; serve those with far-future immutable cache headers.
WHITENOISE_IMMUTABLE_FILE_TEST = vite_immutable_file
# In dev, serve straight from the finders (no collectstatic needed, no missing-dir warning).
WHITENOISE_USE_FINDERS = DEBUG
WHITENOISE_AUTOREFRESH = DEBUG


# Celery (config/celery.py reads every CELERY_* setting)
# https://docs.celeryq.dev/en/stable/userguide/configuration.html

CELERY_BROKER_URL = env.CELERY_BROKER_URL
CELERY_RESULT_BACKEND = env.CELERY_RESULT_BACKEND or env.CELERY_BROKER_URL
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TIMEZONE = TIME_ZONE
# Report STARTED (not just PENDING → SUCCESS) so clients can tell "queued" from "running".
CELERY_TASK_TRACK_STARTED = True
# Eager mode (opt-in, tests): tasks run inline in the caller. Failures are stored on the result
# like a worker would (state FAILURE + exception) instead of raising into the request.
CELERY_TASK_ALWAYS_EAGER = env.CELERY_EAGER
CELERY_TASK_EAGER_PROPAGATES = False
# Durability: nothing is lost when a worker or the broker restarts. Valkey persists the queue
# to disk (docker-compose: appendonly + volume); the settings below make sure a task is only
# removed from the queue once it has finished, so a crash mid-task means "run again", not "gone".
# Consequence: tasks must be idempotent (they may execute twice after a crash).
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
# One task per worker process at a time: with acks_late, prefetched tasks would sit unacked in a
# worker that may die (they come back only after `visibility_timeout`).
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_TRANSPORT_OPTIONS = {
    # Redis has no real acks: an unacked task is redelivered after this many seconds. Must be
    # longer than the longest task (the `count` sample allows 600 × 10 s), otherwise it runs twice.
    "visibility_timeout": 2 * 60 * 60,
}
# Periodic tasks (`./scripts/celery.sh maintenance`). File-based on purpose: the schedule is
# code, reviewed and deployed like code. Times are in CELERY_TIMEZONE (UTC).
CELERY_BEAT_SCHEDULE = {
    "nightly-database-backup": {
        "task": "apps.core.tasks.backup_database",
        "schedule": crontab(hour=3, minute=0),
    },
}
# Cross-tenant maintenance (the backup dumps every user's rows) runs on its own queue, consumed
# only by the maintenance worker that connects as the table owner (`celery worker -B -Q
# maintenance` with DB_ROLE=migrator — docker/docker-compose.prod.yml `beat`). The regular
# workers run as `app_user` and never see these tasks; row-level security would hide the data.
CELERY_TASK_ROUTES = {"apps.core.tasks.backup_database": {"queue": "maintenance"}}


# Email — EMAIL_URL (console output when unset; production needs smtp://…, see config/env.py)
# https://docs.djangoproject.com/en/6.1/topics/email/#topic-email-configuration

MAILERS = {"default": django_mailer(env.EMAIL_URL)}
DEFAULT_FROM_EMAIL = env.DEFAULT_FROM_EMAIL
SERVER_EMAIL = env.DEFAULT_FROM_EMAIL  # sender of Django's error mails, should ADMINS ever be set


# Error monitoring — Sentry, only with SENTRY_DSN (Django, Celery, Redis and logging integrations
# are picked up automatically). https://docs.sentry.io/platforms/python/integrations/django/

if env.SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=env.SENTRY_DSN,
        release=env.APP_VERSION,
        environment="development" if DEBUG else "production",
        traces_sample_rate=env.SENTRY_TRACES_SAMPLE_RATE,
        # Never attach users, cookies or request bodies to events (this app holds patient data).
        send_default_pii=False,
    )


# System checks — `manage.py check --deploy`, run by docker/entrypoint.sh before serving. Only two
# explicit decisions are silenced: HTTPS off (plain-http smoke test of the production image) and
# the deliberate `dummy://` mailer; nothing else ever is.

SILENCED_SYSTEM_CHECKS: list[str] = []
if not HTTPS_ONLY:
    SILENCED_SYSTEM_CHECKS += ["security.W004", "security.W008", "security.W012", "security.W016"]
if env.EMAIL_URL and env.EMAIL_URL.startswith("dummy:"):
    SILENCED_SYSTEM_CHECKS.append("mail.E001")
