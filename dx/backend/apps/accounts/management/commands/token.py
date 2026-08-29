"""`manage.py token` — print an access token for curl/scripts.

TOKEN=$(uv run python manage.py token)
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/auth/me

The token expires (`--minutes`, default ACCESS_TOKEN_LIFETIME_MINUTES); for anything that
runs unattended create a personal API token instead (`POST /api/auth/api-tokens`).
"""

from datetime import timedelta

import djclick as click

from apps.accounts import services


@click.command()
@click.option("--username", "-u", default="admin", show_default=True)
@click.option("--password", "-p", default="admin", show_default=True)
@click.option("--minutes", "-m", type=int, default=None, help="Lifetime; default: the setting")
def command(username: str, password: str, minutes: int | None) -> None:
    """Print a JWT access token for a user. Output is the bare token (no newline)."""
    try:
        user = services.login(username, password)
    except services.InvalidCredentials:
        raise click.ClickException("Invalid credentials") from None
    lifetime = timedelta(minutes=minutes) if minutes is not None else None
    click.echo(services.issue_access_token(user, lifetime=lifetime), nl=False)
