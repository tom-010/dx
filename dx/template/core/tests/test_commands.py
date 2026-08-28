import pytest
from click.testing import CliRunner

from core.management.commands import createadmin
from core.models import User


@pytest.mark.infra
class CommandTest:
    pytestmark = pytest.mark.django_db
    runner = CliRunner()


class TestCreateAdmin(CommandTest):
    def test_minimal(self):
        assert User.objects.count() == 0
        self.runner.invoke(createadmin.command, [])
        assert User.objects.count() == 1
        assert User.objects.first().username == "admin"
        assert User.objects.first().email
        assert User.objects.first().is_superuser
        self.runner.invoke(createadmin.command, [])
        self.runner.invoke(createadmin.command, [])
        self.runner.invoke(createadmin.command, [])
        assert User.objects.count() == 1  # no effect

    def test_create_another_admin(self):
        assert User.objects.count() == 0
        self.runner.invoke(createadmin.command, [])
        self.runner.invoke(createadmin.command, ["--username", "user2", "--email", "hi@email.com"])
        assert User.objects.count() == 2
        user = User.objects.get(username="user2")
        assert user.email == "hi@email.com"
        assert user.is_superuser is True
