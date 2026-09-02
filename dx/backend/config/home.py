"""The development front door at `/`.

In development the SPA is served by Vite on :5173, so Django's own `/` falls through to the SPA
catch-all (`config/spa.py`) and, without a build, answers "frontend not built" — a dead end
exactly where the first click of a fresh checkout lands. This puts the handful of things this
process actually serves there instead, one link each.

Behind a login, like every page it links to: the admin and the explorer read tenant data, and
the session this page shows is the one they read. So it says who that is and offers the one
button that ends it — logging out of the admin from a page that is not the admin is otherwise a
URL you have to remember.

Mounted only while `DEV_HOME_ENABLED` (`config/urls.py`, defaults to DEBUG); in production `/`
is the SPA and this module is never imported. It borrows the explorer's stylesheet rather than
growing one of its own: same kind of tool, same look, and still no build step between a dev and
the page they are looking at.
"""

from collections.abc import Iterator

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.views import redirect_to_login
from django.http import HttpRequest, HttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect, resolve_url
from django.templatetags.static import static
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.views.decorators.http import require_POST

#: The Vite dev server (`./scripts/frontend.sh`). The port is fixed — `--strictPort` makes Vite
#: fail rather than quietly move to 5174 — so this link is either right or nothing is running.
FRONTEND_URL = "http://localhost:5173/"

PAGE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>dx · development</title>
    <link rel="stylesheet" href="{css}">
  </head>
  <body>
    <header>
      <nav aria-label="Breadcrumb"><ol><li>dx</li></ol></nav>
    </header>
    <main>
      <h1>dx</h1>
      <p>The development server. Everything below is development only: in production
        <code>/</code> is the app itself and none of these pages are mounted.</p>
      <form method="post" action="{logout_url}">
        <input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">
        <span>Signed in as <code>{username}</code>.</span>
        <button type="submit">Log out</button>
      </form>
      <dl>{links}</dl>
    </main>
  </body>
</html>
"""


def _links() -> Iterator[tuple[str, str, str]]:
    """`(href, label, what it is)` — what there is to open, most-used first.

    The admin's switch is checked because it really can be off while DEBUG is on
    (`ADMIN_ENABLED`), and ninja hides the API docs behind the same flag (`config/api.py`):
    without the admin there is no way to get the staff session they require, so they go too.
    """
    yield FRONTEND_URL, "Frontend", "The SPA, served by Vite (./scripts/frontend.sh)."
    if settings.ADMIN_ENABLED:
        yield "/api/docs", "API docs", "The OpenAPI spec, browsable. Needs a staff session."
    if settings.EXPLORER_ENABLED:
        yield "/explorer/", "Explorer", "Every model: its rows, versions and lineage. Staff only."
    if settings.ADMIN_ENABLED:
        yield "/admin/", "Admin", "Django's admin over the logged-in user's rows."


def dev_home(request: HttpRequest) -> HttpResponse:
    user = request.user
    if not user.is_authenticated:
        # The admin's login form is the only one there is; `LOGIN_URL` is what is left when the
        # admin is off, the same fallback the explorer uses (apps/core/explorer.py).
        login_url = "admin:login" if settings.ADMIN_ENABLED else settings.LOGIN_URL
        return redirect_to_login(request.get_full_path(), resolve_url(login_url))
    links = format_html_join("\n", '<dt><a href="{}">{}</a></dt><dd>{}</dd>', _links())
    return HttpResponse(
        format_html(
            PAGE,
            css=static("explorer/explorer.css"),
            logout_url=reverse("dev-logout"),
            csrf_token=get_token(request),
            username=user.get_username(),
            links=links,
        )
    )


@require_POST
def dev_logout(request: HttpRequest) -> HttpResponse:
    """End the session and come back to `/`, which then asks for a login again.

    A form and not a link, because Django's own `LogoutView` is POST-only for a good reason: a
    GET that logs you out is one any prefetch can follow. Its own view rather than the admin's,
    so the button still works when `ADMIN_ENABLED` is off.
    """
    logout(request)
    return redirect("dev-home")
