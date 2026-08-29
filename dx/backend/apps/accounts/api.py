"""HTTP surface for authentication and personal API tokens (tag `auth`)."""

import uuid
from datetime import datetime

from django.http import HttpRequest
from ninja import Field, ModelSchema, Router, Schema, Status
from ninja.errors import HttpError

from apps.accounts import services
from apps.accounts.auth import current_user
from apps.accounts.models import ApiToken, ApiTokenId, User
from apps.core.schemas import StrictSchema

router = Router(tags=["auth"])


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


@router.post("/auth/login", response=TokenOut, auth=None)
def login(request: HttpRequest, credentials: LoginIn) -> TokenOut:
    try:
        user = services.login(credentials.username, credentials.password)
    except services.InvalidCredentials:
        raise HttpError(401, "Invalid username or password") from None
    return _token_pair(services.issue_tokens(user))


def _token_pair(pair: services.TokenPair) -> TokenOut:
    return TokenOut(access_token=pair.access_token, refresh_token=pair.refresh_token)


@router.post("/auth/register", response={201: TokenOut}, auth=None)
def register(request: HttpRequest, payload: RegisterIn) -> Status[TokenOut]:
    """Self-service sign-up; only available when `REGISTRATION_OPEN=true`."""
    try:
        user = services.register(**payload.model_dump())
    except services.RegistrationClosed:
        raise HttpError(403, "Registration is closed") from None
    except services.UserAlreadyExists as exc:
        raise HttpError(409, str(exc)) from None
    return Status(201, _token_pair(services.issue_tokens(user)))


@router.get("/auth/me", response=UserOut)
def get_current_user(request: HttpRequest) -> User:
    return current_user(request)


@router.post("/auth/refresh", response=TokenOut, auth=None)
def refresh_token(request: HttpRequest, payload: RefreshTokenIn) -> TokenOut:
    """Trade a refresh token for a new access + refresh pair (the old refresh token is revoked).

    Public: the access token is usually expired by the time this is called; the refresh token
    in the body is the credential.
    """
    try:
        return _token_pair(services.rotate_refresh_token(payload.refresh_token))
    except services.InvalidRefreshToken:
        raise HttpError(401, "Invalid or expired refresh token") from None


@router.post("/auth/logout", response={204: None}, auth=None)
def logout(request: HttpRequest, payload: RefreshTokenIn) -> Status[None]:
    """End the session: the refresh token stops working (the access token expires by itself).

    Public for the same reason as /auth/refresh; always 204, even for a token that is gone.
    """
    services.revoke_refresh_token(payload.refresh_token)
    return Status(204, None)


@router.get("/auth/api-tokens", response=list[ApiTokenOut])
def list_api_tokens(request: HttpRequest) -> list[ApiToken]:
    return services.list_api_tokens(current_user(request))


@router.post("/auth/api-tokens", response={201: ApiTokenOut})
def create_api_token(request: HttpRequest, payload: ApiTokenIn) -> Status[ApiToken]:
    return Status(201, services.create_api_token(current_user(request), payload.name))


@router.delete("/auth/api-tokens/{token_id}", response={204: None})
def revoke_api_token(request: HttpRequest, token_id: uuid.UUID) -> Status[None]:
    try:
        services.revoke_api_token(current_user(request), ApiTokenId(token_id))
    except services.ApiTokenNotFound:
        raise HttpError(404, "API token not found") from None
    return Status(204, None)
