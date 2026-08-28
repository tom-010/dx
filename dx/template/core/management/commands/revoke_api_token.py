import djclick as click
from django.conf import settings
from core.models import ApiToken
from rich import print


@click.command()
@click.argument('token')
def command(token):
    """Revoke an API token (DEBUG mode only)"""

    if not settings.DEBUG:
        print("[red]Error: API tokens can only be revoked in DEBUG mode![/red]")
        return

    try:
        api_token = ApiToken.objects.get(token=token)

        if not api_token.is_active:
            print(f"[yellow]Token is already revoked.[/yellow]")
            return

        api_token.revoke()
        print(f"[green]✓ Token revoked successfully![/green]")
        print(f"Token: {api_token.token[:20]}...")
        print(f"User: {api_token.user.username}")
        print(f"Name: {api_token.name}")

    except ApiToken.DoesNotExist:
        print(f"[red]Error: Token not found[/red]")
        print("Use 'python manage.py list_api_tokens' to see available tokens.")