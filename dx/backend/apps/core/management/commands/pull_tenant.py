"""`manage.py pull_tenant USERNAME [-o FILE] [--no-scrub] [--with-files]` — one user's data.

Prod → dev parity for a single tenant, and the export half of "back up one user": the user row
plus every owned row, anonymised (apps/core/scrub.py), as one JSON fixture that
`manage.py load_tenant` imports. Runs as the runtime role with the tenant pinned (session
context + ORM scope), so the export cannot contain another user's rows even if a query forgot a
filter — row-level security does not allow it.

`--with-files` bundles the uploaded objects as well, into a zip (fixture + `files/<key>`), which
is what makes the pair a real backup rather than rows naming keys that are not there. File
*contents* cannot be anonymised, so it requires `--no-scrub` — the same explicit choice.
See `docs/tenant-data.md`.
"""

from pathlib import Path

import djclick as click
from django.core.serializers import serialize
from django.db import models
from django_scopes import scope, scopes_disabled
from rich.console import Console

from apps.accounts.models import User
from apps.core import rls, scrub, tenants
from apps.core.db import pin_session_tenant

console = Console()


@click.command()
@click.argument("username")
@click.option(
    "--output", "-o", type=click.Path(dir_okay=False, path_type=Path), default="tenant_dump.json"
)
@click.option(
    "--no-scrub", is_flag=True, help="Skip anonymisation. Requires an explicit legal basis."
)
@click.option(
    "--with-files",
    is_flag=True,
    help="Bundle the uploaded files too, as a zip. Requires --no-scrub.",
)
def command(username: str, output: Path, no_scrub: bool, with_files: bool) -> None:
    """Export USERNAME's user row and owned data as a scrubbed JSON fixture."""
    if with_files and not no_scrub:
        raise click.UsageError(
            "--with-files needs --no-scrub: the rows would be anonymised while the files they "
            "point at are the originals."
        )
    with scopes_disabled():
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise click.ClickException(f"no user named {username!r}") from None

    pin_session_tenant(user.pk)
    objects: list[models.Model] = [user]
    with scope(user=user.pk):
        # Everything the tenant policy protects: owned rows (soft-deleted ones included), the
        # version history mirroring them, and the lineage edges between those versions.
        for model in rls.isolated_models():
            objects.extend(tenants.owned_rows(model, user).iterator())

    if not no_scrub:
        try:
            objects = [scrub.scrub(obj, number) for number, obj in enumerate(objects, 1)]
        except scrub.UnscrubbedField as exc:
            raise click.ClickException(str(exc)) from None

    fixture = serialize("json", objects, indent=2)
    note = " [yellow](not scrubbed)[/yellow]" if no_scrub else ""
    if not with_files:
        output.write_text(fixture)
        console.print(
            f"[green]✓[/green] wrote {len(objects)} object(s) for [bold]{username}[/bold] "
            f"to {output}{note}"
        )
        return

    archive = output.with_suffix(".zip")
    files = tenants.write_archive(archive, fixture, tenants.tenant_files(user))
    console.print(
        f"[green]✓[/green] wrote {len(objects)} object(s) and {files} file(s) for "
        f"[bold]{username}[/bold] to {archive}{note}"
    )
