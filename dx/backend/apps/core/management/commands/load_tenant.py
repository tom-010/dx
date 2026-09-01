"""`manage.py load_tenant FIXTURE` — import what `pull_tenant` wrote.

Takes either form: the plain JSON fixture, or the zip from `pull_tenant --with-files`, whose
uploaded objects are restored to the exact storage keys the rows name (`apps/core/tenants.py`).
See `docs/tenant-data.md`.

Works as the runtime role: the fixture's user becomes the pinned tenant, so the owned rows pass
the policy's WITH CHECK. Rows with the same primary key are overwritten, nothing is deleted.
"""

import json
import tempfile
import uuid
import zipfile
from pathlib import Path

import djclick as click
from django.core.management import call_command
from django_scopes import scopes_disabled
from rich.console import Console

from apps.core import tenants
from apps.core.db import pin_session_tenant
from apps.core.history import unversioned

console = Console()


def fixture_user_id(path: Path) -> uuid.UUID:
    try:
        objects = json.loads(path.read_text())
        users = [obj for obj in objects if obj.get("model") == "accounts.user"]
    except (OSError, ValueError, AttributeError) as exc:
        raise click.ClickException(f"cannot read {path}: {exc}") from None
    if len(users) != 1:
        raise click.ClickException(f"{path} must contain exactly one accounts.user (pull_tenant)")
    return uuid.UUID(str(users[0]["pk"]))


@click.command()
@click.argument("fixture", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def command(fixture: Path) -> None:
    """Load a tenant fixture (or archive) produced by pull_tenant."""
    with tempfile.TemporaryDirectory() as workdir:
        files = 0
        if zipfile.is_zipfile(fixture):
            # Files first: rows that name a key nobody restored are the failure this avoids,
            # while an object nobody points at is a harmless orphan (as in `delete_tenant`).
            try:
                path, files = tenants.unpack_archive(fixture, Path(workdir))
            except tenants.TenantArchiveError as error:
                raise click.ClickException(str(error)) from None
        else:
            path = fixture

        pin_session_tenant(fixture_user_id(path))
        # `unversioned()`: the fixture carries each row's version and its event rows, so the
        # load has to reproduce them rather than bump every version and write history twice.
        with scopes_disabled(), unversioned():
            call_command("loaddata", str(path), verbosity=0)

    console.print(
        f"[green]✓[/green] loaded [bold]{fixture}[/bold]"
        + (f" and restored {files} file(s)" if files else "")
    )
