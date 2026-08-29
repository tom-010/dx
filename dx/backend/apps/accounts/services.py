"""Auth business logic: credentials, token issuing/verification, personal API tokens.

Plain typed Python; no request objects. The ninja layer (`api.py`, `auth.py`) maps the
exceptions raised here to HTTP status codes.
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from django.conf import settings
from django.contrib.auth import authenticate

from apps.accounts.models import API_TOKEN_PREFIX, ApiToken, ApiTokenId, RefreshToken, User
from config.env import env

JWT_ALGORITHM = "HS256"
# The `token_type` claim keeps the two JWT kinds apart: a refresh token must never pass as an
# access token (it lives for weeks) and vice versa.
TokenType = Literal["access", "refresh"]


class InvalidCredentials(Exception):
    pass


class RegistrationClosed(Exception):
    pass


class UserAlreadyExists(Exception):
    """The message is safe to show to the user."""


class ApiTokenNotFound(Exception):
    pass


class InvalidRefreshToken(Exception):
    """Malformed, expired, revoked, or the user is inactive — the client has to log in again."""


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str


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


# --- JWTs: access + refresh ---------------------------------------------------------------------
#
# Login issues a pair. The access token is stateless (HS256 with SECRET_KEY), carried on every
# request and verified without a database hit, so it stays short-lived: it cannot be revoked.
# The refresh token lives for weeks but is tied to a `RefreshToken` row: `rotate_refresh_token`
# swaps it for a new pair and revokes the old row (single-use), `revoke_refresh_token` is
# logout. A stolen refresh token therefore stops working as soon as it is rotated or the
# session is ended, while a stolen access token dies with ACCESS_TOKEN_LIFETIME_MINUTES.


def _encode(payload: dict[str, object]) -> str:
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=JWT_ALGORITHM)


def _decode(token: str, token_type: TokenType) -> dict[str, object] | None:
    """Verified claims of a JWT of the given kind; None for anything invalid or expired."""
    try:
        payload: dict[str, object] = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[JWT_ALGORITHM]
        )
    except jwt.InvalidTokenError:
        return None
    return payload if payload.get("token_type") == token_type else None


def issue_access_token(user: User, *, lifetime: timedelta | None = None) -> str:
    """Short-lived JWT for `Authorization: Bearer` (default ACCESS_TOKEN_LIFETIME_MINUTES)."""
    now = datetime.now(UTC)
    payload = {
        "token_type": "access",
        "sub": str(user.pk),
        "username": user.username,
        "jti": str(uuid.uuid7()),  # unique per token (two issued in one second would collide)
        "iat": now,
        "exp": now + (lifetime or timedelta(minutes=env.ACCESS_TOKEN_LIFETIME_MINUTES)),
    }
    return _encode(payload)


def issue_refresh_token(user: User) -> str:
    """JWT bound to a new `RefreshToken` row (REFRESH_TOKEN_LIFETIME_DAYS)."""
    now = datetime.now(UTC)
    session = RefreshToken.objects.create(
        user=user, expires=now + timedelta(days=env.REFRESH_TOKEN_LIFETIME_DAYS)
    )
    payload = {
        "token_type": "refresh",
        "sub": str(user.pk),
        "jti": str(session.pk),
        "iat": now,
        "exp": session.expires,
    }
    return _encode(payload)


def issue_tokens(user: User) -> TokenPair:
    """A fresh login: access + refresh token. Also drops the user's expired sessions."""
    RefreshToken.objects.filter(user=user, expires__lt=datetime.now(UTC)).delete()
    return TokenPair(issue_access_token(user), issue_refresh_token(user))


def user_from_access_token(token: str) -> User | None:
    payload = _decode(token, "access")
    if payload is None:
        return None
    try:
        return User.objects.get(pk=uuid.UUID(str(payload["sub"])), is_active=True)
    except KeyError, ValueError, User.DoesNotExist:
        return None


def _session_from_refresh_token(token: str) -> RefreshToken:
    payload = _decode(token, "refresh")
    if payload is None:
        raise InvalidRefreshToken
    try:
        return RefreshToken.objects.select_related("user").get(
            pk=uuid.UUID(str(payload["jti"])), user__is_active=True
        )
    except KeyError, ValueError, RefreshToken.DoesNotExist:
        raise InvalidRefreshToken from None


def rotate_refresh_token(token: str) -> TokenPair:
    """Trade a refresh token for a new pair; the old one is revoked (single-use).

    A revoked or expired token is simply rejected. No "reuse detection" (ending every session
    of the user when an old token shows up): two browser tabs refreshing at the same moment
    would trigger it constantly, and a refresh token that leaked via the client's storage
    implies the client itself is compromised.
    """
    session = _session_from_refresh_token(token)
    if not session.is_active or session.expires <= datetime.now(UTC):
        raise InvalidRefreshToken
    session.revoke()
    return TokenPair(issue_access_token(session.user), issue_refresh_token(session.user))


def revoke_refresh_token(token: str) -> None:
    """Logout. Tolerates tokens that are already gone — logging out twice is not an error."""
    try:
        session = _session_from_refresh_token(token)
    except InvalidRefreshToken:
        return
    if session.is_active:
        session.revoke()


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
    """Resolve any bearer token: personal API token (`tk_...`), fixed CI token, or access JWT."""
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
