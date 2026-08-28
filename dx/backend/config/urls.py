"""URL configuration.

Order matters: API, admin and media first, then the SPA catch-all. Static assets are served by
WhiteNoise middleware before URL resolution happens.
"""

from django.contrib import admin
from django.urls import path, re_path

from config.api import api
from config.media import serve_media
from config.spa import spa_index

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", api.urls),
    # Uploaded files, streamed from the object store (signed links, see config/media.py).
    path("media/<path:path>", serve_media, name="media"),
    # Everything else is a client-side route: hand it to the SPA (deep links must work).
    re_path(r"^(?!api/|admin/|static/|media/).*$", spa_index, name="spa-index"),
]

# JSON error bodies for API clients (e.g. unknown /api/... paths), HTML pages otherwise.
handler400 = "config.errors.bad_request"
handler403 = "config.errors.permission_denied"
handler404 = "config.errors.not_found"
handler500 = "config.errors.server_error"
