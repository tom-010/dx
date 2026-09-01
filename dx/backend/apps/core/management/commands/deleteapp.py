"""`manage.py deleteapp NAME` — remove a feature app. The opposite of `newapp`.

    DB_ROLE=migrator uv run python manage.py deleteapp reports
    uv run python manage.py deleteapp reports --keep-data     # code only, tables stay

Undoes what `newapp` did, in the order that keeps the repository and the database in step:

  1. `migrate NAME zero` — drops the app's tables *and* the event tables holding their history,
     with their row-level security policies, and clears its rows from `django_migrations`. This
     has to happen while the migration files still exist, so it comes first.
  2. the two registrations (`INSTALLED_APPS`, `config/api.py`) come out,
  3. `apps/NAME/` is deleted,
  4. `history_schema --write` re-records the tracked field set without it.

Everything it deletes is versioned data (`.claude/rules/versioning.md`) — this is one of the
very few operations that really removes rows, so it asks first. `core` and `accounts` are not
feature apps and are refused.
"""

import subprocess
import sys

import djclick as click
from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.db import DatabaseError
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from apps.core import rls, scaffold
from config.env import BASE_DIR

console = Console()


@click.command()
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Do not ask before deleting.")
@click.option("--keep-data", is_flag=True, help="Leave the tables alone; remove the code only.")
def command(name: str, yes: bool, keep_data: bool) -> None:
    """Delete apps/NAME: its tables, its registration and its files."""
    config = next((c for c in apps.get_app_configs() if c.name == f"apps.{name}"), None)
    if config is None:
        known = sorted(c.label for c in apps.get_app_configs() if c.name.startswith("apps."))
        raise click.ClickException(f"no app named apps.{name}; one of: {', '.join(known)}")
    if config.name in settings.SHARED_APPS:
        raise click.ClickException(
            f"{config.name} is infrastructure, not a feature app (settings.SHARED_APPS)"
        )

    models = sorted(config.get_models(), key=lambda model: model._meta.db_table)
    if not keep_data and rls.connection_bypasses_rls() is None:
        raise click.ClickException(
            "dropping tables needs the role that owns them: "
            f"DB_ROLE=migrator manage.py deleteapp {name} (or --keep-data to keep them)"
        )

    table = Table(title=f"apps.{name}", title_justify="left", show_header=False)
    table.add_column("", style="bold")
    table.add_column("")
    table.add_row("directory", str((BASE_DIR / "apps" / name).relative_to(BASE_DIR)))
    table.add_row("tables", ", ".join(model._meta.db_table for model in models) or "none")
    table.add_row("data", "kept" if keep_data else "[red]dropped, history included[/red]")
    console.print(table)

    if not (yes or click.confirm(f"delete apps.{name}?", default=False)):
        raise click.Abort

    if not keep_data:
        # While the migration files are still there: unapplying is what drops the tables, their
        # event tables and the policies on both.
        try:
            call_command("migrate", name, "zero", verbosity=0)
        except DatabaseError as error:
            raise click.ClickException(f"could not unapply {name}'s migrations: {error}") from None
        console.print(f"[green]✓[/green] dropped {len(models)} table(s)")

    missing = scaffold.unregister_app(
        name, BASE_DIR / "config" / "settings.py", BASE_DIR / "config" / "api.py"
    )
    console.print("[green]✓[/green] unregistered (INSTALLED_APPS, config/api.py)")
    for line in missing:
        console.print(f"[yellow]![/yellow] not found, remove by hand: [dim]{line}[/dim]")

    try:
        files = scaffold.remove_app(name, BASE_DIR / "apps")
    except scaffold.ScaffoldError as error:
        raise click.ClickException(str(error)) from None
    console.print(f"[green]✓[/green] deleted apps/{name}/ ({files} file(s))")

    # A subprocess: this process loaded the app registry while the app still existed, so the
    # snapshot it would write here still names its models.
    subprocess.run(
        [sys.executable, "manage.py", "history_schema", "--write"], cwd=BASE_DIR, check=True
    )

    console.print(
        Panel(
            "Next:\n"
            "  1. ./scripts/sync_schema.sh  → the client without its endpoints\n"
            f"  2. remove the resource from RESOURCES in apps/core/tests/test_ownership.py\n"
            f"  3. frontend: src/routes/{name}.tsx and its nav entry in __root.tsx",
            title=f"apps.{name} deleted",
            expand=False,
        )
    )
