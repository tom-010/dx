"""`manage.py restore NAME | --latest` — load a dump from the backup storage.

Runs `migrate` first, then `loaddata`. Rows with the same primary key are overwritten,
rows that only exist in the current database stay. Dev only unless you know what you do.
"""

import djclick as click
from django.conf import settings
from rich.console import Console

from apps.core import backups

console = Console()


@click.command()
@click.argument("name", required=False)
@click.option("--latest", is_flag=True, help="Restore the newest dump.")
@click.option("--yes", "-y", is_flag=True, help="Do not ask for confirmation.")
def command(name: str | None, latest: bool, yes: bool) -> None:
    """Restore the database from a dump created by `manage.py backup`."""
    if latest:
        newest = backups.latest_backup()
        if newest is None:
            raise click.ClickException("no backups found")
        name = newest.name
    if not name:
        raise click.UsageError("give a backup NAME or --latest (see `backup --list`)")
    database = settings.DATABASES["default"]["NAME"]
    if not yes:
        click.confirm(f"Restore {name} into database {database!r}?", abort=True)
    try:
        backups.restore_backup(name)
    except backups.BackupNotFound:
        raise click.ClickException(f"no such backup: {name}") from None
    console.print(f"[green]✓[/green] restored [bold]{name}[/bold] into {database}")
