"""Serves the built React SPA (frontend/dist/index.html) for all non-API routes."""

from django.conf import settings
from django.http import HttpRequest, HttpResponse


def spa_index(request: HttpRequest, path: str = "") -> HttpResponse:
    index = settings.SPA_INDEX
    if not index.is_file():
        return HttpResponse(
            "Frontend not built. In dev use the Vite server (./scripts/frontend.sh); "
            "for a full build run ./scripts/build.sh.",
            status=503,
            content_type="text/plain",
        )
    response = HttpResponse(index.read_bytes(), content_type="text/html")
    # The HTML must never be cached: it references hashed assets that change per deploy.
    response["Cache-Control"] = "no-cache"
    return response
