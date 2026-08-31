"""`manage.py backup` — dump the database to the backup storage (apps/core/backups.py).

uv run python manage.py backup            # new dump (S3 bucket or backend/backups/)
uv run python manage.py backup --prune    # ...and keep only the newest BACKUP_KEEP
uv run python manage.py backup --list
"""

import djclick as click
from django.conf import settings
from rich.console import Console
from rich.filesize import decimal
from rich.table import Table

from apps.core import backups, rls

console = Console()


@click.command()
@click.option("--list", "list_only", is_flag=True, help="List existing dumps, do not create one.")
@click.option(
    "--prune", is_flag=True, help="After the dump, delete all but the newest BACKUP_KEEP."
)
def command(list_only: bool, prune: bool) -> None:
    """Create a database dump (or list the existing ones)."""
    if list_only:
        table = Table(title="Backups", show_header=True)
        table.add_column("Name", style="bold")
        table.add_column("Size", justify="right")
        table.add_column("Modified")
        for backup in backups.list_backups():
            table.add_row(
                backup.name, decimal(backup.size), backup.modified.isoformat(" ", "seconds")
            )
        console.print(table)
        return

    try:
        backup = backups.create_backup()
    except rls.CrossTenantAccessRequired as exc:
        raise click.ClickException(str(exc)) from None
    console.print(f"[green]✓[/green] wrote [bold]{backup.name}[/bold] ({decimal(backup.size)})")
    if prune:
        deleted = backups.prune_backups(settings.BACKUP_KEEP)
        console.print(f"pruned {len(deleted)} old dump(s), keeping {settings.BACKUP_KEEP}")
