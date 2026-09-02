"""URL configuration.

Order matters: API, admin and media first, then the SPA catch-all. Static assets are served by
WhiteNoise middleware before URL resolution happens.

The catch-all excludes those prefixes **with or without a trailing slash**. `/admin` (no slash)
would otherwise be a client-side route: it does not start with `admin/`, so the SPA answered it
and Django's `APPEND_SLASH` never got the chance to redirect to `/admin/` — a resolved URL is
not a 404, so there was nothing to fix up.
"""

from django.conf import settings
from django.contrib import admin
from django.urls import URLPattern, URLResolver, include, path, re_path

from config.api import api
from config.media import serve_media
from config.spa import spa_index

urlpatterns: list[URLPattern | URLResolver] = []

# The admin is a staff UI over tenant data (apps/core/admin.py) and is not deployed to
# production; `Env.ADMIN_ENABLED` defaults to DEBUG. When it is off the paths below simply do
# not resolve, so /admin/ is a 404 rather than a login page nobody can get past.
if settings.ADMIN_ENABLED:
    urlpatterns += [path("admin/", admin.site.urls)]

# A read-only browser over every model, its versions and its lineage (apps/core/explorer.py).
# Development only, and the views check again themselves: two independent guards, because a
# tool that walks every table must not be one settings mistake away from being served in
# production. `EXPLORER_ENABLED` defaults to DEBUG (config/settings.py).
if settings.EXPLORER_ENABLED:
    from apps.core import explorer

    urlpatterns += [path("explorer/", include((explorer.urlpatterns, "explorer")))]

# `/` in development: the SPA lives on the Vite server then, so Django's own root would be the
# catch-all below, answering "frontend not built". A link list to the pages above instead, behind
# the same login they need, with the button that ends that session next to it (config/home.py).
# `DEV_HOME_ENABLED` defaults to DEBUG (config/settings.py); both paths have to sit above the
# catch-all to be reached at all.
if settings.DEV_HOME_ENABLED:
    from config.home import dev_home, dev_logout

    urlpatterns += [
        path("", dev_home, name="dev-home"),
        path("logout/", dev_logout, name="dev-logout"),
    ]

urlpatterns += [
    path("api/", api.urls),
    # Uploaded files, streamed from the object store (signed links, see config/media.py).
    path("media/<path:path>", serve_media, name="media"),
    # Everything else is a client-side route: hand it to the SPA (deep links must work).
    re_path(r"^(?!(?:api|admin|explorer|static|media)(?:/|$)).*$", spa_index, name="spa-index"),
]

# JSON error bodies for API clients (e.g. unknown /api/... paths), HTML pages otherwise.
handler400 = "config.errors.bad_request"
handler403 = "config.errors.permission_denied"
handler404 = "config.errors.not_found"
handler500 = "config.errors.server_error"
