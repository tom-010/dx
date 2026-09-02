"""`manage.py prune_contents [--days N] [--keep-latest] [--dry-run]` — retire old snapshots.

    DB_ROLE=migrator uv run python manage.py prune_contents --days 30 --dry-run

Retires (soft-deletes) every snapshot that is not current, has finished, and is older than N
days — optionally sparing the latest successful run per extractor and document. It never
touches a current snapshot. Logic: `apps/documents/ops.py`.
"""

import djclick as click
import structlog
from rich.console import Console
from rich.table import Table

from apps.core import rls
from apps.core.db import all_tenants
from apps.documents import ops

console = Console()
log = structlog.get_logger(__name__)


@click.command()
@click.option("--days", type=click.IntRange(0), default=30, show_default=True, help="Older than.")
@click.option(
    "--keep-latest",
    is_flag=True,
    help="Spare the latest successful snapshot of each extractor on each document.",
)
@click.option("--dry-run", is_flag=True, help="List what would be retired, change nothing.")
def command(days: int, keep_latest: bool, dry_run: bool) -> None:
    """Retire non-current, finished snapshots older than --days (the current one never)."""
    # A cleanup across every tenant: no ORM scope, and a connection the policies do not bind.
    with all_tenants():
        rls.require_cross_tenant_access()
        found = list(
            ops.prunable_contents(
                older_than_days=days, keep_latest_per_extractor=keep_latest
            ).select_related("document", "extractor")
        )
        table = Table(title=f"Snapshots older than {days} day(s), not current")
        table.add_column("Document")
        table.add_column("Extractor")
        table.add_column("Status")
        table.add_column("Created")
        for content in found:
            table.add_row(
                str(content.document),
                str(content.extractor),
                content.status,
                f"{content.created:%Y-%m-%d}",
            )
        console.print(table if found else "[dim]nothing to prune[/dim]")
        if dry_run or not found:
            return
        counts = ops.prune_contents(found)
    console.print("retired " + ", ".join(f"{n} {what}" for what, n in counts.items()))
    log.info("contents_pruned", days=days, keep_latest=keep_latest, **counts)
