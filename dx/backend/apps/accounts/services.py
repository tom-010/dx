"""Auth business logic: credentials, token issuing/verification, personal API tokens.

Plain typed Python; no request objects. The ninja layer (`api.py`, `auth.py`) maps the
exceptions raised here to HTTP status codes.
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from django.conf import settings
from django.contrib.auth import authenticate

from apps.accounts.models import API_TOKEN_PREFIX, ApiToken, ApiTokenId, User
from config.env import env

JWT_ALGORITHM = "HS256"


class InvalidCredentials(Exception):
    pass


class RegistrationClosed(Exception):
    pass


class UserAlreadyExists(Exception):
    """The message is safe to show to the user."""


class ApiTokenNotFound(Exception):
    pass


# --- Credentials -------------------------------------------------------------------------------


def login(username: str, password: str) -> User:
    user = authenticate(username=username, password=password)
    if not isinstance(user, User):
        raise InvalidCredentials
    return user


def register(
    *, username: str, email: str, password: str, first_name: str = "", last_name: str = ""
) -> User:
    if not env.REGISTRATION_OPEN:
        raise RegistrationClosed
    if User.objects.filter(username=username).exists():
        raise UserAlreadyExists("Username already taken")
    if email and User.objects.filter(email=email).exists():
        raise UserAlreadyExists("Email already registered")
    return User.objects.create_user(
        username=username,
        email=email,
        password=password,
        first_name=first_name,
        last_name=last_name,
    )


# --- Bearer tokens -----------------------------------------------------------------------------


def issue_access_token(user: User) -> str:
    """Signed JWT (HS256 with SECRET_KEY) that expires after ACCESS_TOKEN_LIFETIME_DAYS."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.pk),
        "username": user.username,
        "iat": now,
        "exp": now + timedelta(days=env.ACCESS_TOKEN_LIFETIME_DAYS),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=JWT_ALGORITHM)


def user_from_access_token(token: str) -> User | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return User.objects.get(pk=uuid.UUID(str(payload["sub"])), is_active=True)
    except jwt.InvalidTokenError, KeyError, ValueError, User.DoesNotExist:
        return None


def user_from_api_token(token: str) -> User | None:
    try:
        api_token = ApiToken.objects.select_related("user").get(
            token=token, is_active=True, user__is_active=True
        )
    except ApiToken.DoesNotExist:
        return None
    api_token.touch()
    return api_token.user


def user_from_fixed_token(token: str) -> User | None:
    """`API_FIXED_TOKEN` from the environment authenticates as the first active superuser."""
    fixed = env.API_FIXED_TOKEN
    if not fixed or not secrets.compare_digest(token, fixed):
        return None
    return User.objects.filter(is_superuser=True, is_active=True).order_by("date_joined").first()


def authenticate_bearer(token: str) -> User | None:
    """Resolve any bearer token: personal API token (`tk_...`), fixed CI token, or JWT."""
    if token.startswith(API_TOKEN_PREFIX):
        return user_from_api_token(token)
    return user_from_fixed_token(token) or user_from_access_token(token)


# --- Personal API tokens -----------------------------------------------------------------------


def list_api_tokens(user: User) -> list[ApiToken]:
    return list(ApiToken.objects.filter(user=user, is_active=True))


def create_api_token(user: User, name: str) -> ApiToken:
    return ApiToken.objects.create(user=user, name=name)


def revoke_api_token(user: User, token_id: ApiTokenId) -> None:
    try:
        api_token = ApiToken.objects.get(pk=token_id, user=user, is_active=True)
    except ApiToken.DoesNotExist as exc:
        raise ApiTokenNotFound(token_id) from exc
    api_token.revoke()
