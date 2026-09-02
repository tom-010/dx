"""Authentication and personal API tokens (tag `auth`): schemas, token handling, endpoints.

Three kinds of bearer token resolve to a user (`authenticate_bearer`): a personal API token
(`tk_…`), the fixed token from the environment (CI), and the short-lived access JWT issued by
`POST /api/auth/login`. `BearerAuth` guards every API operation (installed globally in
`config/api.py`); public operations opt out with `auth=None`.

`apps.core.middleware.TenantMiddleware` verifies the same header before the view and opens the
tenant context with it; `BearerAuth` recognises that (request.user is a `User` whose pk is the
active context) and does not verify the token a second time. A token the middleware rejected is
verified once more here, which yields the same answer: 401.
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from django.conf import settings
from django.contrib.auth import authenticate
from django.http import HttpRequest
from ninja import Field, ModelSchema, Router, Schema, Status
from ninja.errors import HttpError
from ninja.security import HttpBearer

from apps.accounts.models import API_TOKEN_PREFIX, ApiToken, ApiTokenId, RefreshToken, User
from apps.core.db import current_user_id
from apps.core.schemas import StrictSchema
from config.env import env

router = Router(tags=["auth"])

JWT_ALGORITHM = "HS256"
# The `token_type` claim keeps the two JWT kinds apart: a refresh token must never pass as an
# access token (it lives for weeks) and vice versa.
TokenType = Literal["access", "refresh"]


class LoginIn(StrictSchema):
    username: str
    password: str


class RegisterIn(StrictSchema):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=8)
    email: str = ""
    first_name: str = ""
    last_name: str = ""


class TokenOut(Schema):
    """Send `access_token` as `Authorization: Bearer …`; it expires after
    ACCESS_TOKEN_LIFETIME_MINUTES. Then POST `refresh_token` to /auth/refresh for a new pair
    (the refresh token is single-use) — or to /auth/logout to end the session."""

    access_token: str
    refresh_token: str


class RefreshTokenIn(StrictSchema):
    refresh_token: str


class UserOut(Schema):
    # Plain Schema on purpose: ModelSchema would copy Django's translated help texts into the spec.
    id: uuid.UUID
    username: str
    email: str
    first_name: str
    last_name: str
    is_staff: bool


class ApiTokenIn(StrictSchema):
    name: str = Field(min_length=1, max_length=255)


class ApiTokenOut(ModelSchema):
    id: uuid.UUID
    last_used: datetime | None

    class Meta:
        model = ApiToken
        fields = ["id", "name", "token", "created", "last_used"]


# --- JWTs: access + refresh ---------------------------------------------------------------------
#
# Login issues a pair. The access token is stateless (HS256 with SECRET_KEY), carried on every
# request and verified without a database hit, so it stays short-lived: it cannot be revoked.
# The refresh token lives for weeks but is tied to a `RefreshToken` row: /auth/refresh swaps it
# for a new pair and revokes the old row (single-use), /auth/logout revokes it. A stolen refresh
# token therefore stops working as soon as it is rotated or the session is ended, while a stolen
# access token dies with ACCESS_TOKEN_LIFETIME_MINUTES.


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
    session = RefreshToken.create(
        operation=None,
        sources=[],
        user=user,
        expires=now + timedelta(days=env.REFRESH_TOKEN_LIFETIME_DAYS),
    )
    payload = {
        "token_type": "refresh",
        "sub": str(user.pk),
        "jti": str(session.pk),
        "iat": now,
        "exp": session.expires,
    }
    return _encode(payload)


def issue_tokens(user: User) -> TokenOut:
    """A fresh login: access + refresh token. Also drops the user's expired sessions."""
    # A real delete, not a soft one: an expired session is a spent credential, and keeping a
    # row per login forever would grow without bound. RefreshToken is exempt from versioning
    # for the same reason (apps/core/history.py::HISTORY_EXEMPT).
    RefreshToken.objects.filter(user=user, expires__lt=datetime.now(UTC)).hard_delete()
    return TokenOut(access_token=issue_access_token(user), refresh_token=issue_refresh_token(user))


def user_from_access_token(token: str) -> User | None:
    payload = _decode(token, "access")
    if payload is None:
        return None
    try:
        return User.objects.get(pk=uuid.UUID(str(payload["sub"])), is_active=True)
    except KeyError, ValueError, User.DoesNotExist:
        return None


def session_from_refresh_token(token: str) -> RefreshToken | None:
    """The live session a refresh token names, or None when it is malformed, expired, revoked or
    the user is inactive — every one of those means the client has to log in again."""
    payload = _decode(token, "refresh")
    if payload is None:
        return None
    try:
        session = RefreshToken.objects.select_related("user").get(
            pk=uuid.UUID(str(payload["jti"])), user__is_active=True
        )
    except KeyError, ValueError, RefreshToken.DoesNotExist:
        return None
    return session if session.is_active and session.expires > datetime.now(UTC) else None


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


class BearerAuth(HttpBearer):
    def authenticate(self, request: HttpRequest, token: str) -> User | None:
        # A session user (admin cookie) never counts: only the middleware's verified bearer user
        # matches the active tenant context.
        user = request.user
        if isinstance(user, User) and current_user_id.get() == user.pk:
            return user
        verified = authenticate_bearer(token)
        if verified is not None:
            request.user = verified
        return verified


def current_user(request: HttpRequest) -> User:
    """The authenticated user of an operation guarded by `BearerAuth`."""
    user = request.user
    if not isinstance(user, User):  # pragma: no cover - guarded by BearerAuth
        raise AssertionError("current_user() called on an unauthenticated request")
    return user


# --- Endpoints ----------------------------------------------------------------------------------


@router.post("/auth/login", response=TokenOut, auth=None)
def login(request: HttpRequest, credentials: LoginIn) -> TokenOut:
    user = authenticate(username=credentials.username, password=credentials.password)
    if not isinstance(user, User):
        raise HttpError(401, "Invalid username or password")
    return issue_tokens(user)


@router.post("/auth/register", response={201: TokenOut}, auth=None)
def register(request: HttpRequest, payload: RegisterIn) -> Status[TokenOut]:
    """Self-service sign-up; only available when `REGISTRATION_OPEN=true`."""
    if not env.REGISTRATION_OPEN:
        raise HttpError(403, "Registration is closed")
    if User.objects.filter(username=payload.username).exists():
        raise HttpError(409, "Username already taken")
    if payload.email and User.objects.filter(email=payload.email).exists():
        raise HttpError(409, "Email already registered")
    user = User.objects.create_user(**payload.model_dump())
    return Status(201, issue_tokens(user))


@router.get("/auth/me", response=UserOut)
def get_current_user(request: HttpRequest) -> User:
    return current_user(request)


@router.post("/auth/refresh", response=TokenOut, auth=None)
def refresh_token(request: HttpRequest, payload: RefreshTokenIn) -> TokenOut:
    """Trade a refresh token for a new access + refresh pair (the old refresh token is revoked).

    Public: the access token is usually expired by the time this is called; the refresh token
    in the body is the credential.

    Deliberately no "reuse detection" (ending every session of the user when an old token shows
    up): two browser tabs refreshing at the same moment would trigger it constantly, and a
    refresh token that leaked via the client's storage implies the client itself is compromised.
    """
    session = session_from_refresh_token(payload.refresh_token)
    if session is None:
        raise HttpError(401, "Invalid or expired refresh token")
    session.revoke()  # single-use
    return issue_tokens(session.user)


@router.post("/auth/logout", response={204: None}, auth=None)
def logout(request: HttpRequest, payload: RefreshTokenIn) -> Status[None]:
    """End the session: the refresh token stops working (the access token expires by itself).

    Public for the same reason as /auth/refresh; always 204, even for a token that is gone —
    logging out twice is not an error.
    """
    session = session_from_refresh_token(payload.refresh_token)
    if session is not None:
        session.revoke()
    return Status(204, None)


@router.get("/auth/api-tokens", response=list[ApiTokenOut])
def list_api_tokens(request: HttpRequest) -> list[ApiToken]:
    return list(ApiToken.objects.filter(user=current_user(request), is_active=True))


@router.post("/auth/api-tokens", response={201: ApiTokenOut})
def create_api_token(request: HttpRequest, payload: ApiTokenIn) -> Status[ApiToken]:
    token = ApiToken.create(
        operation=None, sources=[], user=current_user(request), name=payload.name
    )
    return Status(201, token)


@router.delete("/auth/api-tokens/{token_id}", response={204: None})
def revoke_api_token(request: HttpRequest, token_id: uuid.UUID) -> Status[None]:
    try:
        api_token = ApiToken.objects.get(
            pk=ApiTokenId(token_id), user=current_user(request), is_active=True
        )
    except ApiToken.DoesNotExist:
        raise HttpError(404, "API token not found") from None
    api_token.revoke()
    return Status(204, None)
