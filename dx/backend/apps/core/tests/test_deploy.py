"""Deployability: the settings must pass Django's deployment checklist once DEBUG is off.

`docker/entrypoint.sh` runs the same `check --deploy --fail-level WARNING` before serving, so a
regression here means the production image refuses to start. Runs in a fresh interpreter so
the environment — not the test settings — is what gets checked (~0.5 s, marker `infra`).
"""

import os
import secrets
import subprocess
import sys

import pytest

from config.env import BASE_DIR

pytestmark = pytest.mark.infra

PRODUCTION_ENV = {
    "DJANGO_SETTINGS_MODULE": "config.settings",
    "DEBUG": "false",
    "ALLOWED_HOSTS": '["dx.example.com"]',
    "EMAIL_URL": "smtp://smtp.example.com",
}


def test_check_deploy_passes_with_a_production_environment() -> None:
    env = {**os.environ, **PRODUCTION_ENV, "SECRET_KEY": secrets.token_hex(32)}
    check = ["check", "--deploy", "--fail-level", "WARNING", "--tag", "security", "--tag", "mail"]

    result = subprocess.run(
        [sys.executable, "manage.py", *check],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_entrypoint_gates_every_deploy_on_the_rls_policies() -> None:
    """Invariant: `migrate` → `rls_sync` → `rls_sync --check`, as the table owner. The check
    exits non-zero on drift, so a container with unprotected tables never starts serving."""
    entrypoint = (BASE_DIR.parent / "docker" / "entrypoint.sh").read_text()
    steps = [
        "DB_ROLE=migrator python manage.py migrate --noinput",
        "DB_ROLE=migrator python manage.py rls_sync",
        "DB_ROLE=migrator python manage.py rls_sync --check",
    ]

    positions = [entrypoint.find(step) for step in steps]

    assert all(position > 0 for position in positions), f"missing step: {steps}"
    assert positions == sorted(positions), "the RLS steps must run after migrate, in order"
