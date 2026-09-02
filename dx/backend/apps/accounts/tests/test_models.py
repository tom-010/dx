import pytest
from ninja import Schema

from apps.accounts.models import ApiToken, User


class TokenPayload(Schema):
    name: str
    is_active: bool = True


@pytest.mark.django_db
def test_base_model_payload_helpers(user: User) -> None:
    token = ApiToken.create(operation=None, sources=[], user=user, name="old", is_active=False)

    # Full payload (PUT): fields the client omitted take the schema defaults.
    token.set_payload(TokenPayload(name="full"))
    assert (token.name, token.is_active) == ("full", True)

    # Partial payload (PATCH): only what the client actually sent changes.
    token.is_active = False
    token.set_payload_partial(TokenPayload(name="partial"))
    assert (token.name, token.is_active) == ("partial", False)

    token.save(operation=None, sources=[])
    token.refresh_from_db()
    assert token.name == "partial"
    assert token.created <= token.modified
    assert str(token) == f"{user} - partial"


@pytest.mark.django_db
def test_user_model_is_the_custom_one(user: User) -> None:
    assert user.pk.version == 7  # UUIDv7 like every other model
    assert user.api_tokens.count() == 0  # reverse relation from ApiToken.user
