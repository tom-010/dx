"""`manage.py rls_sync [--check]` — row-level security policies on every tenant-data table.

    ./scripts/migrate.sh                       # migrate + rls_sync + rls_sync --check
    DB_ROLE=migrator uv run python manage.py rls_sync
    uv run python manage.py rls_sync --check   # exit 1 on drift; gates every deploy

Runs as the table owner (DB_ROLE=migrator); `--check` works as any role. Logic: apps/core/rls.py.
"""

import djclick as click
from django.db import DatabaseError
from rich.console import Console

from apps.core import rls

console = Console()


@click.command()
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    help="Verify only: exit 1 if a table lacks RLS or its policy. Applies nothing.",
)
def command(check_only: bool) -> None:
    """Create/refresh the RLS policies (or verify them with --check).

    Covers every table with an owner column: the owned models, the event tables holding their
    history, and the lineage graph (apps/core/rls.py::isolated_models).
    """
    tables = rls.isolated_tables()
    if not check_only:
        role = rls.current_role()  # before sync(): a failed DDL leaves no working transaction
        try:
            changed = rls.sync(tables=tables)
        except DatabaseError as exc:
            raise click.ClickException(
                f"{exc}\nrls_sync changes policies and grants, which only the table owner may "
                f"do: run it as DB_ROLE=migrator (./scripts/migrate.sh); current role: {role}"
            ) from None
        if changed:
            console.print(
                f"[green]✓[/green] RLS policy [bold]{rls.POLICY}[/bold] (re)created on "
                f"{len(changed)} table(s): {', '.join(changed)}"
            )
        else:
            console.print("[dim]RLS policies already up to date, nothing changed[/dim]")
    problems = rls.verify(tables=tables)
    if problems:
        raise click.ClickException("RLS drift detected:\n  " + "\n  ".join(problems))
    console.print(f"[green]✓[/green] RLS OK on {len(tables)} table(s): {', '.join(tables)}")
