from datetime import datetime, timedelta
from typing import Optional
import os
import jwt
from django.conf import settings
from django.contrib.auth import authenticate
from ninja import Router, Schema
from ninja.security import HttpBearer, APIKeyHeader
from core.models import User, ApiToken
from django.utils import timezone

class JWTAuth(HttpBearer):
    def authenticate(self, request, token):
        # 1. Check if it's a user-specific fixed token (DEBUG mode only)
        if settings.DEBUG and token.startswith('tk_'):
            try:
                api_token = ApiToken.objects.select_related('user').get(
                    token=token,
                    is_active=True
                )
                # Update last_used timestamp
                api_token.touch()
                request.user = api_token.user
                request.api_token = api_token  # Store token info for logging
                return api_token.user
            except ApiToken.DoesNotExist:
                pass

        # 2. Check if it's a fixed API token from environment
        fixed_token = os.environ.get("API_FIXED_TOKEN")
        if fixed_token and token == fixed_token:
            # Use a system user or the first superuser
            try:
                user = User.objects.filter(is_superuser=True).first()
                if not user:
                    user = User.objects.get(username=os.environ.get("API_FIXED_USER", "admin"))
                request.user = user
                return user
            except User.DoesNotExist:
                pass

        # 3. Otherwise, try JWT authentication
        try:
            payload = jwt.decode(
                token,
                settings.SECRET_KEY,
                algorithms=["HS256"]
            )
            user = User.objects.get(id=payload["user_id"])
            request.user = user
            return user
        except (jwt.InvalidTokenError, User.DoesNotExist):
            return None

class LoginSchema(Schema):
    username: str
    password: str

class TokenSchema(Schema):
    access_token: str
    token_type: str = "Bearer"

class UserSchema(Schema):
    id: str
    username: str
    email: str
    first_name: str
    last_name: str

class ErrorSchema(Schema):
    error: str

api = Router()
jwt_auth = JWTAuth()

def create_token(user: User) -> str:
    payload = {
        "user_id": user.id,
        "username": user.username,
        "exp": datetime.utcnow() + timedelta(days=7),
        "iat": datetime.utcnow()
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")

@api.post("/token", response={200: TokenSchema, 401: ErrorSchema}, auth=None)
def get_token(request, credentials: LoginSchema):
    user = authenticate(
        request,
        username=credentials.username,
        password=credentials.password
    )
    if user:
        token = create_token(user)
        return 200, TokenSchema(access_token=token)
    return 401, ErrorSchema(error="Invalid credentials")

@api.get("/user", response=UserSchema)
def get_current_user(request):
    return UserSchema(
        id=request.user.id,
        username=request.user.username,
        email=request.user.email,
        first_name=request.user.first_name,
        last_name=request.user.last_name
    )

@api.post("/refresh", response={200: TokenSchema})
def refresh_token(request):
    token = create_token(request.user)
    return 200, TokenSchema(access_token=token)

@api.get("/verify")
def verify_token(request):
    return {"valid": True, "user_id": request.user.id}

class RegisterSchema(Schema):
    username: str
    email: str
    password: str
    first_name: str = ""
    last_name: str = ""

@api.post("/register", response={201: TokenSchema, 400: ErrorSchema}, auth=None)
def register(request, data: RegisterSchema):
    try:
        if User.objects.filter(username=data.username).exists():
            return 400, ErrorSchema(error="Username already exists")

        if User.objects.filter(email=data.email).exists():
            return 400, ErrorSchema(error="Email already exists")

        user = User.objects.create_user(
            username=data.username,
            email=data.email,
            password=data.password,
            first_name=data.first_name,
            last_name=data.last_name
        )

        token = create_token(user)
        return 201, TokenSchema(access_token=token)
    except Exception as e:
        return 400, ErrorSchema(error=str(e))

# API Token Management (DEBUG mode only)
class ApiTokenSchema(Schema):
    id: str
    name: str
    token: str
    is_active: bool
    created: datetime
    last_used: Optional[datetime]

class CreateApiTokenSchema(Schema):
    name: str

class ApiTokenListSchema(Schema):
    tokens: list[ApiTokenSchema]

@api.post("/api-tokens", response={201: ApiTokenSchema, 403: ErrorSchema})
def create_api_token(request, data: CreateApiTokenSchema):
    """Create a fixed API token for the current user (DEBUG mode only)"""
    if not settings.DEBUG:
        return 403, ErrorSchema(error="API tokens are only available in DEBUG mode")

    api_token = ApiToken.objects.create(
        user=request.user,
        name=data.name
    )

    return 201, ApiTokenSchema(
        id=api_token.id,
        name=api_token.name,
        token=api_token.token,
        is_active=api_token.is_active,
        created=api_token.created,
        last_used=api_token.last_used
    )

@api.get("/api-tokens", response={200: ApiTokenListSchema, 403: ErrorSchema})
def list_api_tokens(request):
    """List API tokens for the current user (DEBUG mode only)"""
    if not settings.DEBUG:
        return 403, ErrorSchema(error="API tokens are only available in DEBUG mode")

    tokens = ApiToken.objects.filter(user=request.user, is_active=True).order_by('-created')

    return 200, ApiTokenListSchema(
        tokens=[
            ApiTokenSchema(
                id=token.id,
                name=token.name,
                token=token.token,
                is_active=token.is_active,
                created=token.created,
                last_used=token.last_used
            )
            for token in tokens
        ]
    )

@api.delete("/api-tokens/{token_id}", response={204: None, 403: ErrorSchema, 404: ErrorSchema})
def revoke_api_token(request, token_id: str):
    """Revoke an API token (DEBUG mode only)"""
    if not settings.DEBUG:
        return 403, ErrorSchema(error="API tokens are only available in DEBUG mode")

    try:
        api_token = ApiToken.objects.get(id=token_id, user=request.user)
        api_token.revoke()
        return 204, None
    except ApiToken.DoesNotExist:
        return 404, ErrorSchema(error="Token not found")