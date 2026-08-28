import djclick as click
from django.conf import settings
from core.models import User, ApiToken
from rich import print
from rich.table import Table


@click.command()
@click.option(
    '--username', '-u',
    default='admin',
    help='Username to create token for (default: admin)'
)
@click.option(
    '--name', '-n',
    default='CLI Token',
    help='Name/description for the token'
)
def command(username, name):
    """Create a fixed API token for a user (DEBUG mode only)"""

    if not settings.DEBUG:
        print("[red]Error: API tokens can only be created in DEBUG mode![/red]")
        print("Set DEBUG=True in your settings to use this feature.")
        return

    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        print(f"[red]Error: User '{username}' not found[/red]")
        return

    # Create the token
    api_token = ApiToken.objects.create(
        user=user,
        name=name
    )

    print(f"[green]✓ API token created successfully![/green]")
    print()
    print(f"[bold]Token:[/bold] {api_token.token}")
    print(f"[bold]User:[/bold] {user.username}")
    print(f"[bold]Name:[/bold] {api_token.name}")
    print()
    print("[yellow]Usage examples:[/yellow]")
    print(f"  export API_TOKEN=\"{api_token.token}\"")
    print(f"  curl -H \"Authorization: Bearer {api_token.token}\" http://localhost:8000/api/auth/user")
    print()
    print("[dim]Note: This token only works when DEBUG=True[/dim]")