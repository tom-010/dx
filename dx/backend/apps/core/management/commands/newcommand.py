"""`manage.py newcommand NAME [--app APP]` — scaffold a management command in one of the apps.

    manage.py newcommand purge_old            # asks which app it belongs in
    manage.py newcommand purge_old --app datasets

Writes `apps/<app>/management/commands/<name>.py` from `backend/scaffold/command.py.tmpl` — the
project's command shape: django-click, rich output, a structured log line, and `all_tenants()`
so it is not tenant-filtered (see `.claude/rules/management-commands.md`).

A command lives in the app it belongs to; `core` is for infrastructure, not the default.
"""

import subprocess

import djclick as click
from django.apps import apps
from rich.console import Console

from apps.core.scaffold import ScaffoldError, render_command
from config.env import BASE_DIR

console = Console()


def project_apps() -> list[str]:
    """The labels of this project's own apps ("core", "datasets", …), in a stable order."""
    return sorted(
        config.label for config in apps.get_app_configs() if config.name.startswith("apps.")
    )


@click.command()
@click.argument("name")
@click.option("--app", "app_label", help="Which app it belongs in; asked for when not given.")
def command(name: str, app_label: str | None) -> None:
    """Create a management command from the project's template."""
    labels = project_apps()
    if app_label is None:
        console.print(f"apps: [bold]{', '.join(labels)}[/bold]")
        app_label = click.prompt(
            "which app does this command belong in?",
            type=click.Choice(labels),
            default="core",
            show_choices=False,
        )
    if app_label not in labels:
        raise click.UsageError(f"unknown app {app_label!r}; one of: {', '.join(labels)}")

    try:
        written = render_command(name, BASE_DIR / "apps" / app_label)
    except ScaffoldError as error:
        raise click.ClickException(str(error)) from None

    subprocess.run(["ruff", "format", str(written)], check=False, capture_output=True)
    console.print(f"[green]✓[/green] {written.relative_to(BASE_DIR)}")
    console.print(f"  run it: [bold]manage.py {name}[/bold] · find it: [bold]manage.py tui[/bold]")
