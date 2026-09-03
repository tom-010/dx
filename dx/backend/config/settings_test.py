"""Settings for the test suite (`DJANGO_SETTINGS_MODULE` in pyproject.toml).

Identical to `config.settings` except for what makes tests hermetic and fast; keep the list
short so tests stay honest about production behaviour.
"""

import tempfile

from config.env import django_database, env
from config.settings import *  # noqa: F403  # extends the real settings on purpose

# The suite connects as the table owner: it creates the test database and migrates it. Each
# database test then switches to the runtime role (`SET ROLE app_user`, backend/conftest.py), so
# row-level security is enforced in tests exactly as in production. Always Postgres itself, never
# the pooler: creating and dropping databases and `SET ROLE` are session-level, and a transaction
# pooler would still hold idle connections to a database the runner is trying to drop.
DATABASES = {
    "default": django_database(env.DATABASE_URL, credentials=env.database_credentials("migrator")),
}

# Celery: run tasks inline, keep results in memory — no broker, no Redis, but
# `GET /api/tasks/{id}` still finds eager results.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = False
CELERY_TASK_STORE_EAGER_RESULT = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"

# Files: local disk (tests set MEDIA_ROOT to a tmp dir) — no object store needed. The S3 backend
# itself is covered by apps/documents/tests/test_s3.py (marker `slow`, needs the compose `s3`).
# Backups go to a throwaway directory so no test ever touches backend/backups or a bucket.
STORAGES = {
    **STORAGES,  # noqa: F405
    "default": LOCAL_STORAGE,  # noqa: F405
    "backups": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": tempfile.mkdtemp(prefix="dx-test-backups-")},
    },
}

# Cache: in-process, so sessions (cached_db) and anything cached need no Valkey.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Password hashing dominates `create_user()` otherwise (~100 ms per user with PBKDF2).
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
