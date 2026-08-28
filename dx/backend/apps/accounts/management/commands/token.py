"""`manage.py token` — print an access token for curl/scripts.

TOKEN=$(uv run python manage.py token)
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/auth/me
"""

import djclick as click

from apps.accounts import services


@click.command()
@click.option("--username", "-u", default="admin", show_default=True)
@click.option("--password", "-p", default="admin", show_default=True)
def command(username: str, password: str) -> None:
    """Print a JWT access token for a user. Output is the bare token (no newline)."""
    try:
        user = services.login(username, password)
    except services.InvalidCredentials:
        raise click.ClickException("Invalid credentials") from None
    click.echo(services.issue_access_token(user), nl=False)
