"""Error responses for API clients.

Django and ninja render errors as HTML by default; the SPA's transport (`custom-fetch.ts`)
expects `{"detail": ...}` JSON. Two layers:

- `install_exception_handlers(api)`: unhandled exceptions inside ninja operations. In DEBUG
  they become a JSON body with the traceback (instead of ninja's text/plain dump); in
  production they are re-raised so Django logs them and `server_error` below answers.
- `bad_request` / `permission_denied` / `not_found` / `server_error`: Django's handler40x/500
  (`config/urls.py`), e.g. for unknown `/api/...` paths. JSON for API requests, Django's
  default pages for everything else.
"""

import logging
import traceback
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views import defaults
from ninja import NinjaAPI

logger = logging.getLogger(__name__)


def wants_json(request: HttpRequest) -> bool:
    return request.path.startswith("/api/") or "application/json" in request.headers.get(
        "Accept", ""
    )


def install_exception_handlers(api: NinjaAPI) -> None:
    @api.exception_handler(Exception)
    def unhandled_exception(request: HttpRequest, exc: Exception) -> HttpResponse:
        if not settings.DEBUG:
            raise exc  # Django's handler500 (`server_error`) takes over and logs it.
        logger.exception("Unhandled error in %s %s", request.method, request.path)
        return JsonResponse(
            {
                "detail": f"{type(exc).__name__}: {exc}",
                "traceback": "".join(traceback.format_exception(exc)).splitlines(),
            },
            status=500,
        )


def _error(
    request: HttpRequest,
    status: int,
    detail: str,
    fallback: Callable[..., HttpResponse],
    *args: object,
) -> HttpResponse:
    if wants_json(request):
        return JsonResponse({"detail": detail}, status=status)
    return fallback(request, *args)


def bad_request(request: HttpRequest, exception: Exception) -> HttpResponse:
    return _error(request, 400, "Bad Request", defaults.bad_request, exception)


def permission_denied(request: HttpRequest, exception: Exception) -> HttpResponse:
    return _error(request, 403, "Permission Denied", defaults.permission_denied, exception)


def not_found(request: HttpRequest, exception: Exception) -> HttpResponse:
    return _error(request, 404, "Not Found", defaults.page_not_found, exception)


def server_error(request: HttpRequest) -> HttpResponse:
    return _error(request, 500, "Internal Server Error", defaults.server_error)
