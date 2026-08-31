"""`manage.py history_schema [--write]` — the checked-in snapshot of every tracked field set.

    uv run python manage.py history_schema            # show it
    uv run python manage.py history_schema --write    # regenerate backend/history_schema.json

Adding or dropping a tracked field changes what a version row means, and old rows cannot say so
by themselves — that is what `SCHEMA_TAG` is for (apps/core/history.py). The snapshot turns
"remember to bump the tag" into a failing test: `apps/core/tests/test_history.py` recomputes it
and refuses a mismatch.
"""

import json

import djclick as click
from rich.console import Console

from apps.core import history

console = Console()


@click.command()
@click.option("--write", "write", is_flag=True, help="Rewrite backend/history_schema.json.")
def command(write: bool) -> None:
    """Print the tracked field set of every versioned model, or write it to the snapshot file."""
    schema = history.tracked_schema()
    payload = json.dumps(schema, indent=2, sort_keys=True) + "\n"

    if not write:
        click.echo(payload, nl=False)
        return

    unchanged = history.SCHEMA_FILE.exists() and history.SCHEMA_FILE.read_text() == payload
    history.SCHEMA_FILE.write_text(payload)
    where = history.SCHEMA_FILE.name
    if unchanged:
        console.print(f"[dim]{where} already up to date (schema tag {schema['current']})[/dim]")
        return
    console.print(
        f"[green]✓[/green] wrote {where} for schema tag [bold]{schema['current']}[/bold]"
        f" ({len(schema['tags'])} tag(s) on record)"
        " — bump SCHEMA_TAG in the same change if the field set moved."
    )
