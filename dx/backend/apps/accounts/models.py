import secrets
import uuid
from typing import NewType

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from apps.core.models import BaseModel

ApiTokenId = NewType("ApiTokenId", uuid.UUID)
RefreshTokenId = NewType("RefreshTokenId", uuid.UUID)

# Distinguishes personal API tokens from JWTs in the Authorization header.
API_TOKEN_PREFIX = "tk_"


class User(AbstractUser):
    """The project's user model (`AUTH_USER_MODEL = "accounts.User"`).

    Django's stock fields (username, email, names, staff/superuser flags, `date_joined`) plus a
    UUIDv7 pk like every other model. Extend it here; never add a second profile table for
    things every user has.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)

    class Meta(AbstractUser.Meta):
        ordering = ["username"]


def generate_token() -> str:
    return API_TOKEN_PREFIX + secrets.token_hex(20)


class ApiToken(BaseModel):
    """Long-lived personal bearer token for scripts and CI (`Authorization: Bearer tk_...`).

    Unlike the JWTs from `POST /api/auth/login` these never expire; revoke them instead.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=255, help_text="What the token is used for")
    token = models.CharField(max_length=64, unique=True, default=generate_token, editable=False)
    is_active = models.BooleanField(default=True)
    last_used = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.user} - {self.name}"

    def revoke(self) -> None:
        self.is_active = False
        self.save(update_fields=["is_active", "modified"])

    def touch(self) -> None:
        self.last_used = timezone.now()
        self.save(update_fields=["last_used"])


class RefreshToken(BaseModel):
    """One login session: the server-side half of a refresh JWT (its `jti` is this row's id).

    Access tokens are stateless and short-lived; the refresh token that renews them is checked
    against this row, so a login can be ended (logout, admin) before the JWT expires. Rotation
    revokes the row and creates a new one on every refresh — a refresh token is single-use.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="refresh_tokens")
    expires = models.DateTimeField()
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"{self.user} - {self.pk}"

    def revoke(self) -> None:
        self.is_active = False
        self.save(update_fields=["is_active", "modified"])
