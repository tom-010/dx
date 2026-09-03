"""`manage.py rebuild_timeline [--type KEY] [--user USERNAME] [--dry-run]`

    uv run python manage.py rebuild_timeline
    uv run python manage.py rebuild_timeline --type documents.uploaded --user tom

Recomputes the projection from the rows it projects: every object in an event type's
`backfill()` gets its event written or refreshed, and events whose source is no longer in there
are retired. Idempotent, so it is both the migration path for data that predates an event type
(run it once after the deploy that adds one) and the repair for a projection that drifted.

The timeline is tenant data, so the rebuild runs once per user inside that user's context —
row-level security means there is no other way to see their rows. Conventions:
`.claude/rules/management-commands.md`.
"""

import djclick as click
import structlog
from rich.console import Console
from rich.table import Table

from apps.accounts.models import User
from apps.core.db import all_tenants, tenant_context
from apps.timeline import services
from apps.timeline.contracts import UnknownEventType, registry

console = Console()
log = structlog.get_logger(__name__)


@click.command()
@click.option("--type", "key", default=None, help="Rebuild one event type instead of all.")
@click.option("--user", "username", default=None, help="Rebuild one user's feed.")
@click.option("--dry-run", is_flag=True, help="Report what would be written, change nothing.")
def command(key: str | None, username: str | None, dry_run: bool) -> None:
    """Recompute timeline events from the rows they project."""
    if key is not None:
        try:
            registry.get(key)
        except UnknownEventType as error:
            raise click.ClickException(str(error)) from error

    with all_tenants():
        users = User.objects.all()
        if username is not None:
            users = users.filter(username=username)
        targets = list(users.order_by("username"))
    if not targets:
        raise click.ClickException(f"No such user: {username}" if username else "No users")

    table = Table(title="Timeline rebuild" + (" (dry run)" if dry_run else ""))
    table.add_column("User")
    table.add_column("Event type")
    table.add_column("Recorded", justify="right")
    table.add_column("Retired", justify="right")
    total = 0
    for user in targets:
        with tenant_context(user.pk):
            # A dry run still needs the counts, so it does the work and rolls it back — the
            # projection is derived data, and a transaction is cheaper than a second code path
            # that reports what the real one would have done.
            counts = _rebuild(key, dry_run=dry_run)
        for event_key, (recorded, retired) in sorted(counts.items()):
            table.add_row(user.username, event_key, str(recorded), str(retired))
            total += recorded
    console.print(table if total or not dry_run else "[dim]nothing to rebuild[/dim]")
    log.info("timeline_rebuilt", users=len(targets), recorded=total, dry_run=dry_run)


def _rebuild(key: str | None, *, dry_run: bool) -> dict[str, tuple[int, int]]:
    from django.db import transaction  # noqa: PLC0415 - only the dry run needs it

    if not dry_run:
        return services.rebuild(key=key)
    counts: dict[str, tuple[int, int]] = {}
    try:
        with transaction.atomic():
            counts = services.rebuild(key=key)
            raise _Rollback
    except _Rollback:
        pass
    return counts


class _Rollback(Exception):
    """Undoes the dry run's transaction once it has counted what it would have done."""
