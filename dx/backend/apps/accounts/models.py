import secrets
import uuid
from datetime import timedelta
from typing import NewType

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from apps.core.examples import unique
from apps.core.models import ActiveManager, VersionedModel
from config.env import env

ApiTokenId = NewType("ApiTokenId", uuid.UUID)
RefreshTokenId = NewType("RefreshTokenId", uuid.UUID)

# Distinguishes personal API tokens from JWTs in the Authorization header.
API_TOKEN_PREFIX = "tk_"


class Language(models.TextChoices):
    """The languages a user's data can be in. Explicit per user, never guessed from a browser
    header: it decides how their text is analysed for search (stemming, stop words — one
    OpenSearch index per user, built for this language; `apps/search/index.py`) and which
    translation the API answers in where it has one."""

    GERMAN = "de", "Deutsch"
    ENGLISH = "en", "English"


class User(AbstractUser):
    """The project's user model (`AUTH_USER_MODEL = "accounts.User"`).

    Django's stock fields (username, email, names, staff/superuser flags, `date_joined`) plus a
    UUIDv7 pk like every other model. Extend it here; never add a second profile table for
    things every user has.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    #: The language of this user's data (`Language`). German by default: that is what the
    #: records this app is built for are written in.
    language = models.CharField(max_length=5, choices=Language.choices, default=Language.GERMAN)

    class Meta(AbstractUser.Meta):
        ordering = ["username"]

    @staticmethod
    def example() -> User:
        # No password: an example user is something to own rows, not something to log in as
        # (`create_user` is that). The username is unique table-wide, hence `unique()`.
        return User(
            username=unique("example"),
            email="example@example.com",
            first_name="Example",
            last_name="User",
        )


def generate_token() -> str:
    return API_TOKEN_PREFIX + secrets.token_hex(20)


class ApiToken(VersionedModel):
    """Long-lived personal bearer token for scripts and CI (`Authorization: Bearer tk_...`).

    Unlike the JWTs from `POST /api/auth/login` these never expire; revoke them instead.
    """

    # Direct `VersionedModel` subclasses declare the manager pair themselves (see VersionedModel).
    objects = ActiveManager()
    all_objects = models.Manager()

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=255, help_text="What the token is used for")
    token = models.CharField(max_length=64, default=generate_token, editable=False)
    is_active = models.BooleanField(default=True)
    last_used = models.DateTimeField(null=True, blank=True)

    class Meta(VersionedModel.Meta):
        # Conditional, not `unique=True`: rows are only ever soft-deleted, and a plain unique
        # index would let a deleted row reserve its value forever.
        constraints = [
            models.UniqueConstraint(
                fields=["token"],
                condition=models.Q(deleted_at__isnull=True),
                name="uniq_active_api_token",
            )
        ]

    @staticmethod
    def example() -> ApiToken:
        return ApiToken(user=User.example(), name="CI deploy key")

    def __str__(self) -> str:
        return f"{self.user} - {self.name}"

    def revoke(self) -> None:
        self.is_active = False
        self.save(operation=None, sources=[], update_fields=["is_active"])

    def touch(self) -> None:
        self.last_used = timezone.now()
        self.save(operation=None, sources=[], update_fields=["last_used"])


class RefreshToken(VersionedModel):
    """One login session: the server-side half of a refresh JWT (its `jti` is this row's id).

    Access tokens are stateless and short-lived; the refresh token that renews them is checked
    against this row, so a login can be ended (logout, admin) before the JWT expires. Rotation
    revokes the row and creates a new one on every refresh — a refresh token is single-use.
    """

    objects = ActiveManager()
    all_objects = models.Manager()

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="refresh_tokens")
    expires = models.DateTimeField()
    is_active = models.BooleanField(default=True)

    @staticmethod
    def example() -> RefreshToken:
        return RefreshToken(
            user=User.example(),
            expires=timezone.now() + timedelta(days=env.REFRESH_TOKEN_LIFETIME_DAYS),
        )

    def __str__(self) -> str:
        return f"{self.user} - {self.pk}"

    def revoke(self) -> None:
        self.is_active = False
        self.save(operation=None, sources=[], update_fields=["is_active"])
