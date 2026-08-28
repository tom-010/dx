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
    """Send as `Authorization: Bearer <access_token>`."""

    access_token: str


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
    return TokenOut(access_token=services.issue_access_token(user))


@router.post("/auth/register", response={201: TokenOut}, auth=None)
def register(request: HttpRequest, payload: RegisterIn) -> Status[TokenOut]:
    """Self-service sign-up; only available when `REGISTRATION_OPEN=true`."""
    try:
        user = services.register(**payload.model_dump())
    except services.RegistrationClosed:
        raise HttpError(403, "Registration is closed") from None
    except services.UserAlreadyExists as exc:
        raise HttpError(409, str(exc)) from None
    return Status(201, TokenOut(access_token=services.issue_access_token(user)))


@router.get("/auth/me", response=UserOut)
def get_current_user(request: HttpRequest) -> User:
    return current_user(request)


@router.post("/auth/refresh", response=TokenOut)
def refresh_token(request: HttpRequest) -> TokenOut:
    """Issue a fresh access token for the caller (call before the current one expires)."""
    return TokenOut(access_token=services.issue_access_token(current_user(request)))


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
