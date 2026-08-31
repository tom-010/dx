"""`manage.py startmodule NAME` — scaffold a feature module (apps/core/scaffold.py).

    uv run python manage.py startmodule reports              # model Report
    uv run python manage.py startmodule inventory --model Item

Creates apps/<name>/ (owned model, admin, tests and one api.py holding the schemas, the logic
and a router with paginated list + get/POST/PUT/PATCH/DELETE), registers it in INSTALLED_APPS
and config/api.py (at the `# needle:` comments) and runs makemigrations. Then:
./scripts/sync_schema.sh for the frontend client.
"""

import subprocess
import sys

import djclick as click
from rich.console import Console
from rich.panel import Panel

from apps.core import scaffold
from config.env import BASE_DIR

console = Console()


@click.command()
@click.argument("name")
@click.option("--model", default=None, help="Model class (default: singular CamelCase of NAME).")
@click.option("--no-migrations", is_flag=True, help="Skip `makemigrations NAME`.")
def command(name: str, model: str | None, no_migrations: bool) -> None:
    """Create apps/NAME from the module template and register it."""
    try:
        spec = scaffold.module_spec(name, model)
        files = scaffold.render_module(spec, BASE_DIR / "apps")
        scaffold.register_module(
            spec, BASE_DIR / "config" / "settings.py", BASE_DIR / "config" / "api.py"
        )
    except scaffold.ScaffoldError as exc:
        raise click.ClickException(str(exc)) from None

    for file in files:
        console.print(f"[green]+[/green] {file.relative_to(BASE_DIR)}")
    console.print(f"[green]✓[/green] registered apps.{spec.name} (INSTALLED_APPS, config/api.py)")

    if not no_migrations:
        # A subprocess: this process loaded settings before the app existed.
        subprocess.run(
            [sys.executable, "manage.py", "makemigrations", spec.name], cwd=BASE_DIR, check=True
        )
        # The new model is versioned (@tracked), so it belongs in the tracked-field snapshot;
        # a subprocess again, for the same reason.
        subprocess.run(
            [sys.executable, "manage.py", "history_schema", "--write"], cwd=BASE_DIR, check=True
        )
    # Django's generated migration is not ruff-formatted (import order); fix the whole app once.
    app_dir = str(BASE_DIR / "apps" / spec.name)
    subprocess.run(["ruff", "check", "--fix", "--quiet", app_dir], cwd=BASE_DIR, check=False)
    subprocess.run(["ruff", "format", "--quiet", app_dir], cwd=BASE_DIR, check=False)

    console.print(
        Panel(
            f"Model [bold]{spec.model}[/bold] at [bold]/api/{spec.name}[/bold]\n\n"
            "Next:\n"
            f"  1. edit apps/{spec.name}/models.py (+ makemigrations) and schemas.py\n"
            "  2. ./scripts/migrate.sh  → tables + the RLS policy for the model and its "
            "history\n"
            "  3. manage.py history_schema --write (after any field change)\n"
            "  4. ./scripts/sync_schema.sh  → typed client in frontend/src/api/\n"
            f"  5. add the resource to RESOURCES in apps/core/tests/test_ownership.py\n"
            f"  6. frontend: src/routes/{spec.name}.tsx + nav entry in __root.tsx",
            title="module created",
            expand=False,
        )
    )
