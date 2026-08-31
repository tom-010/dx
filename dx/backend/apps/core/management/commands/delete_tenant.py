"""`manage.py delete_tenant USERNAME [-y]` — erase one user and all of their data.

Irreversible: the user row, every owned row and the files they reference are gone. Shows what
it is about to delete and asks first. Needs cross-tenant database credentials (the cascade has
to see the rows row-level security hides from the runtime role):

    DB_ROLE=migrator uv run python manage.py delete_tenant alice

Take a `manage.py backup` (or `pull_tenant alice`) first if the data might be wanted again.
Deleting a user in the Django admin is disabled on purpose — see apps/core/tenants.py.
"""

import djclick as click
from django_scopes import scopes_disabled
from rich.console import Console
from rich.table import Table

from apps.accounts.models import User
from apps.core import rls, tenants

console = Console()


@click.command()
@click.argument("username")
@click.option("--yes", "-y", is_flag=True, help="Do not ask for confirmation.")
def command(username: str, yes: bool) -> None:
    """Delete USERNAME and everything they own (rows and stored files)."""
    with scopes_disabled():
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise click.ClickException(f"no user named {username!r}") from None

    try:
        summary = tenants.tenant_summary(user)
    except rls.CrossTenantAccessRequired as exc:
        raise click.ClickException(str(exc)) from None

    table = Table(title=f"About to delete {username} and all their data", show_header=True)
    table.add_column("Model", style="bold")
    table.add_column("Rows", justify="right")
    for label, count in summary.items():
        table.add_row(label, str(count))
    console.print(table)
    if not yes:
        click.confirm(
            f"Permanently delete {username} and {sum(summary.values())} row(s)?", abort=True
        )

    erasure = tenants.delete_tenant(user)
    console.print(
        f"[green]✓[/green] deleted [bold]{erasure.username}[/bold]: "
        f"{erasure.total_rows} row(s), {erasure.files} file(s)"
    )
