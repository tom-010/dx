"""`manage.py hello_world [NAME]` — the reference management command; copy it for new ones.

Conventions for commands in this project:
- django-click (`import djclick as click`): arguments/options are click decorators, `--help`
  and validation come for free, the function is named `command`. Django's own `--settings`,
  `--pythonpath`, `--traceback`, `-v` still work.
- rich for output (`Console`, `Table`, `Panel`, progress bars); `click.echo` for bare output
  that scripts capture (see `token`).
- User-facing failures: raise `click.ClickException("...")` (exit 1, red message).
- Logic that is more than plumbing goes to a service module; the command only parses input
  and prints. Long-running work: enqueue a Celery task instead.
- Tests: `click.testing.CliRunner().invoke(module.command, [...])` (not `call_command`, which
  does not know click options) — see apps/core/tests/test_commands.py.

    uv run python manage.py hello_world            # Hello, world!
    uv run python manage.py hello_world dx --shout -n 2
"""

import djclick as click
import structlog
from django.conf import settings
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from apps.accounts.models import User

console = Console()
log = structlog.get_logger(__name__)


@click.command()
@click.argument("name", default="world")
@click.option("--shout", is_flag=True, help="Upper-case the greeting.")
@click.option(
    "--repeat", "-n", type=click.IntRange(1, 10), default=1, show_default=True, help="Say it n×."
)
def command(name: str, shout: bool, repeat: int) -> None:
    """Greet NAME and show a few facts about this environment."""
    greeting = f"Hello, {name}!"
    if shout:
        greeting = greeting.upper()
    for _ in range(repeat):
        console.print(Panel(greeting, title="dx", expand=False))

    table = Table(title="Environment", show_header=False)
    table.add_column("Key", style="bold")
    table.add_column("Value")
    table.add_row("DEBUG", str(settings.DEBUG))
    table.add_row("database", settings.DATABASES["default"]["NAME"])
    table.add_row("media storage", settings.STORAGES["default"]["BACKEND"].rsplit(".", 1)[-1])
    table.add_row("log format", settings.LOG_FORMAT)
    table.add_row("users", str(User.objects.count()))
    console.print(table)

    # Structured log line (key/value pairs, no f-strings) — see config/logging.py.
    log.info("hello_world_ran", name=name, shout=shout, repeat=repeat)
