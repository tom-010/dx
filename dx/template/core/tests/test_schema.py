import pytest

import core.schema as s
from core.models import User


class TestUserSchema:
    pytestmark = pytest.mark.django_db

    def test_no_password(self):
        user = User.example()
        user.set_password("password")
        schema = s.UserSchema.from_orm(user)
        serialized = schema.dict()
        assert "password" not in serialized

    def test_email_validation(self):
        user = User.example()
        user.email = "invalid"
        with pytest.raises(ValueError):
            s.UserSchema.from_orm(user)
