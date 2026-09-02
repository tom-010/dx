"""`manage.py gc_blobs [--dry-run]` — retire blobs nothing references any more.

    DB_ROLE=migrator uv run python manage.py gc_blobs --dry-run

Checks all five referencing columns — `Document.source_blob`, `Document.thumbnail`,
`DocumentContent.blob`, `DocumentContent.raw_output`, `Page.thumbnail` — over every row of
those tables, soft-deleted ones included. Logic: `apps/documents/ops.py`.
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
@click.option("--dry-run", is_flag=True, help="List the orphans, change nothing.")
def command(dry_run: bool) -> None:
    """Retire blob rows no document, snapshot or page points at."""
    with all_tenants():
        rls.require_cross_tenant_access()
        orphans = list(ops.orphan_blobs())
        table = Table(title="Orphaned blobs")
        table.add_column("Owner")
        table.add_column("Type")
        table.add_column("Size", justify="right")
        table.add_column("sha256")
        for blob in orphans:
            table.add_row(str(blob.owner_id), blob.mime_type, str(blob.size), blob.sha256[:16])
        console.print(table if orphans else "[dim]no orphaned blobs[/dim]")
        if dry_run or not orphans:
            return
        count = ops.gc_blobs(orphans)
    console.print(f"retired {count} blob(s)")
    log.info("blobs_collected", count=count)
