import djclick as click
from django.conf import settings
from core.models import User, ApiToken
from rich import print
from rich.table import Table
from rich.console import Console


@click.command()
@click.option(
    '--username', '-u',
    help='Filter tokens by username'
)
@click.option(
    '--all', '-a',
    is_flag=True,
    help='Show all tokens (including inactive)'
)
def command(username, all):
    """List API tokens (DEBUG mode only)"""

    if not settings.DEBUG:
        print("[red]Error: API tokens can only be viewed in DEBUG mode![/red]")
        return

    # Build query
    query = ApiToken.objects.select_related('user')

    if username:
        query = query.filter(user__username=username)

    if not all:
        query = query.filter(is_active=True)

    tokens = query.order_by('-created')

    if not tokens.exists():
        print("[yellow]No API tokens found.[/yellow]")
        if not all:
            print("Use --all to show inactive tokens as well.")
        return

    # Create table
    console = Console()
    table = Table(title="API Tokens", show_header=True, header_style="bold magenta")
    table.add_column("Token", style="cyan", no_wrap=True)
    table.add_column("User", style="green")
    table.add_column("Name", style="yellow")
    table.add_column("Active", style="blue")
    table.add_column("Created", style="dim")
    table.add_column("Last Used", style="dim")

    for token in tokens:
        table.add_row(
            token.token[:20] + "..." if len(token.token) > 20 else token.token,
            token.user.username,
            token.name,
            "✓" if token.is_active else "✗",
            token.created.strftime("%Y-%m-%d %H:%M"),
            token.last_used.strftime("%Y-%m-%d %H:%M") if token.last_used else "Never"
        )

    console.print(table)
    print()
    print(f"[dim]Total: {tokens.count()} token(s)[/dim]")