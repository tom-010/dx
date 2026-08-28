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
