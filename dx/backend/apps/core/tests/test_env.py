"""config/env.py: the URL → Django translations and the production guards."""

import pytest
from pydantic import PostgresDsn, ValidationError
from pydantic_settings import SettingsConfigDict

from config.env import DEV_SECRET_KEY, Env, django_database, django_mailer

STRONG_KEY = "k" * 50


class IsolatedEnv(Env):
    """`Env` without the developer's backend/.env, so a test sees only what it passes."""

    model_config = SettingsConfigDict(env_file=None)


def test_database_url_maps_to_django_with_psycopg_options() -> None:
    url = PostgresDsn("postgres://dx:p%40ss@db.example.com:5433/prod?sslmode=require")

    assert django_database(url, conn_max_age=60) == {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "prod",
        "USER": "dx",
        "PASSWORD": "p@ss",
        "HOST": "db.example.com",
        "PORT": 5433,
        "OPTIONS": {"sslmode": "require"},
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "ATOMIC_REQUESTS": False,
        "DISABLE_SERVER_SIDE_CURSORS": False,
    }


def test_pooled_database_disables_server_side_cursors() -> None:
    url = PostgresDsn("postgres://app_user:runtime@pgbouncer:6432/dx")

    assert django_database(url, pooled=True)["DISABLE_SERVER_SIDE_CURSORS"] is True


def test_database_url_follows_the_pooler_switch_and_the_role() -> None:
    direct = PostgresDsn("postgres://app_user:runtime@db:5432/dx")
    pooled = PostgresDsn("postgres://app_user:runtime@pgbouncer:6432/dx")

    unpooled = IsolatedEnv(DATABASE_URL=direct, DATABASE_POOL_URL=pooled)
    assert unpooled.database_url() == direct  # DB_POOLED off: Postgres itself
    assert unpooled.pooled() is False

    web = IsolatedEnv(DATABASE_URL=direct, DATABASE_POOL_URL=pooled, DB_POOLED=True)
    assert web.database_url() == pooled
    assert web.pooled() is True
    # Maintenance roles never go through the pooler, whatever the process says.
    assert web.database_url("migrator") == direct
    assert web.database_url("admin") == direct
    assert web.pooled("migrator") is False

    without_pooler = IsolatedEnv(DATABASE_URL=direct, DATABASE_POOL_URL=None, DB_POOLED=True)
    with pytest.raises(ValueError, match="DATABASE_POOL_URL"):
        without_pooler.database_url()


def test_database_credentials_follow_the_role() -> None:
    url = PostgresDsn("postgres://app_user:runtime@db/dx")
    env = IsolatedEnv(DATABASE_URL=url, DB_MIGRATOR_USER="app_migrator", DB_MIGRATOR_PASSWORD="m")

    assert env.database_credentials() is None  # DB_ROLE=app: the URL's own
    assert env.database_credentials("migrator") == ("app_migrator", "m")
    with pytest.raises(ValueError, match="DB_ADMIN_USER"):
        env.database_credentials("admin")

    database = django_database(url, credentials=env.database_credentials("migrator"))
    assert (database["USER"], database["PASSWORD"], database["NAME"]) == ("app_migrator", "m", "dx")


def test_email_url_unset_prints_to_the_console() -> None:
    console = {"BACKEND": "django.core.mail.backends.console.EmailBackend"}

    assert django_mailer(None) == console
    assert django_mailer("") == console


def test_email_url_smtp_with_starttls() -> None:
    mailer = django_mailer("smtp://postmaster%40example.com:p%40ss@smtp.example.com?tls=true")

    assert mailer == {
        "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
        "OPTIONS": {
            "host": "smtp.example.com",
            "port": 587,
            "username": "postmaster@example.com",
            "password": "p@ss",
            "use_tls": True,
            "use_ssl": False,
            "timeout": 10,
        },
    }


def test_email_url_smtps_uses_implicit_tls() -> None:
    options = django_mailer("smtps://smtp.example.com")["OPTIONS"]

    assert (options["port"], options["use_ssl"], options["use_tls"]) == (465, True, False)


def test_email_url_dummy_backend_is_an_explicit_choice() -> None:
    assert django_mailer("dummy://") == {"BACKEND": "django.core.mail.backends.dummy.EmailBackend"}


def test_email_url_rejects_unknown_schemes() -> None:
    with pytest.raises(ValueError, match="EMAIL_URL"):
        django_mailer("imap://mail.example.com")


def test_env_refuses_the_dev_secret_key_without_debug() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        IsolatedEnv(DEBUG=False, SECRET_KEY=DEV_SECRET_KEY)


def test_env_https_only_follows_debug_unless_set() -> None:
    assert IsolatedEnv(DEBUG=True).https_only is False
    assert IsolatedEnv(DEBUG=False, SECRET_KEY=STRONG_KEY).https_only is True

    explicit = IsolatedEnv(DEBUG=False, SECRET_KEY=STRONG_KEY, HTTPS_ONLY=False)
    assert explicit.https_only is False
