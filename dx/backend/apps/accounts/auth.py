"""ninja authentication: `Authorization: Bearer <token>` on every API operation.

Installed globally in `config/api.py`; public operations opt out with `auth=None`.
"""

from django.http import HttpRequest
from ninja.security import HttpBearer

from apps.accounts import services
from apps.accounts.models import User


class BearerAuth(HttpBearer):
    def authenticate(self, request: HttpRequest, token: str) -> User | None:
        user = services.authenticate_bearer(token)
        if user is not None:
            request.user = user
        return user


def current_user(request: HttpRequest) -> User:
    """The authenticated user of an operation guarded by `BearerAuth`."""
    user = request.user
    if not isinstance(user, User):  # pragma: no cover - guarded by BearerAuth
        raise AssertionError("current_user() called on an unauthenticated request")
    return user
