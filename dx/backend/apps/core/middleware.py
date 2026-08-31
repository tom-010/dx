"""Request tenant context: verify the bearer token once, then run the request inside a
transaction with the tenant set (`SET LOCAL app.user_id` + ORM scope, apps/core/db.py).

Requests without a valid token proceed with **no** context: every owned table is then empty
and unwritable (the policy fails closed), and ninja's `BearerAuth` answers 401 anyway. The
identity comes only from the verified token — never from a header, query parameter or body
field a client could set.
"""

from collections.abc import Callable

import structlog
from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase

from apps.accounts import api as accounts_api
from apps.accounts.models import User
from apps.core.db import tenant_context

log = structlog.get_logger(__name__)

# Only the API authenticates with bearer tokens; the admin uses sessions, /media and /static
# are signed or public. Nothing else is given a tenant context.
API_PREFIX = "/api/"


def bearer_user(request: HttpRequest) -> User | None:
    """The user behind `Authorization: Bearer <token>`, parsed the way ninja's HttpBearer does
    (so the middleware and `BearerAuth` always agree on the token).

    Reached through the module rather than a direct name so both call sites — here and
    `BearerAuth` — resolve the same function at call time (test_tenancy counts the calls).
    """
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return accounts_api.authenticate_bearer(token)


class TenantMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        if not request.path.startswith(API_PREFIX):
            return self.get_response(request)
        user = bearer_user(request)
        if user is None:
            return self.get_response(request)

        request.user = user  # BearerAuth reuses it — the token is verified exactly once
        with tenant_context(user.pk):
            response = self.get_response(request)
        if response.streaming and not isinstance(response, HttpResponse):
            # The transaction (and with it the context) is gone before the body is consumed:
            # queries inside a streaming generator would see nothing. Materialise first.
            log.warning("tenant_streaming_response", path=request.path)
        return response
