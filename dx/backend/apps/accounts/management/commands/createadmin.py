"""`manage.py createadmin` — idempotent dev superuser (default admin / admin)."""

import djclick as click
from rich.console import Console

from apps.accounts.models import User

console = Console()


@click.command()
@click.option("--username", "-u", default="admin", show_default=True)
@click.option("--email", "-e", default=None, help="Default: <username>@example.com")
@click.option("--password", "-p", default=None, help="Default: same as the username")
def command(username: str, email: str | None, password: str | None) -> None:
    """Create a superuser for local development if it does not exist."""
    if User.objects.filter(username=username).exists():
        console.print(f"User [bold]{username}[/bold] already exists")
        return
    User.objects.create_superuser(
        username=username,
        email=email or f"{username}@example.com",
        password=password or username,
    )
    console.print(f"[green]✓[/green] Created superuser [bold]{username}[/bold]")
