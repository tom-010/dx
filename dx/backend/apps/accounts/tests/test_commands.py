import pytest
from click.testing import CliRunner

from apps.accounts import services
from apps.accounts.management.commands import createadmin, token
from apps.accounts.models import User

pytestmark = pytest.mark.django_db

runner = CliRunner()


def test_createadmin_is_idempotent() -> None:
    first = runner.invoke(createadmin.command, [])
    second = runner.invoke(createadmin.command, [])

    assert first.exit_code == 0, first.output
    assert "Created superuser admin" in first.output
    assert "already exists" in second.output
    (admin,) = User.objects.all()
    assert admin.username == "admin"
    assert admin.email == "admin@example.com"
    assert admin.is_superuser
    assert admin.check_password("admin")


def test_createadmin_with_options() -> None:
    result = runner.invoke(createadmin.command, ["-u", "ops", "-e", "ops@dx.test", "-p", "s3cret"])

    assert result.exit_code == 0, result.output
    ops = User.objects.get(username="ops")
    assert ops.email == "ops@dx.test"
    assert ops.check_password("s3cret")


def test_token_prints_a_bare_access_token() -> None:
    runner.invoke(createadmin.command, [])

    result = runner.invoke(token.command, [])

    assert result.exit_code == 0, result.output
    user = services.user_from_access_token(result.output)
    assert user is not None
    assert user.username == "admin"


def test_token_fails_for_wrong_credentials() -> None:
    result = runner.invoke(token.command, ["-u", "nobody", "-p", "nothing"])

    assert result.exit_code == 1
    assert "Invalid credentials" in result.output
