import os
import subprocess
import sys
from pathlib import Path

import djclick as click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, Prompt

console = Console()


def snake_to_camel(snake_str):
    """Convert snake_case to CamelCase"""
    components = snake_str.split('_')
    return ''.join(x.title() for x in components)


def update_settings(app_name):
    """Add the app to INSTALLED_APPS in settings.py"""
    settings_path = Path("config/settings.py")
    content = settings_path.read_text()

    needle = "    # needle: add-module-to-settings"
    if needle not in content:
        console.print("[red]✗[/red] Could not find needle comment in config/settings.py")
        return False

    # Add the app before the needle
    new_line = f'    "{app_name}",\n'
    content = content.replace(needle, new_line + needle)

    settings_path.write_text(content)
    console.print(f"[green]✓[/green] Added '{app_name}' to INSTALLED_APPS")
    return True


def update_urls(app_name):
    """Add the app router to urls.py"""
    urls_path = Path("config/urls.py")
    content = urls_path.read_text()

    # Check if needle exists
    needle = "# needle: add-api-router"
    if needle not in content:
        console.print("[red]✗[/red] Could not find needle comment in config/urls.py")
        return False

    # Add import at the top (after other app imports)
    import_line = f"from {app_name} import api as {app_name}_api"

    # Find where to add the import (after the last "from X import api" line)
    lines = content.split('\n')
    import_index = -1
    for i, line in enumerate(lines):
        if "import api as" in line and "from" in line:
            import_index = i

    if import_index != -1:
        lines.insert(import_index + 1, import_line)

    # Add router registration before the needle
    router_line = f'api.add_router("/{app_name}", {app_name}_api.api, tags=["{app_name}"])'
    content = '\n'.join(lines)
    content = content.replace(needle, router_line + "\n" + needle)

    urls_path.write_text(content)
    console.print(f"[green]✓[/green] Added router for '{app_name}' to urls.py")
    return True


def run_migrations(app_name):
    """Run makemigrations for the new app"""
    try:
        console.print(f"\n[cyan]Running migrations for {app_name}...[/cyan]")

        # Run makemigrations
        result = subprocess.run(
            [sys.executable, "manage.py", "makemigrations", app_name],
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            console.print(f"[green]✓[/green] Created migrations for '{app_name}'")

            # Show migration output
            if result.stdout:
                console.print(result.stdout)

            # Ask if user wants to run migrate
            if Confirm.ask("\nDo you want to run migrations now?"):
                migrate_result = subprocess.run(
                    [sys.executable, "manage.py", "migrate"],
                    capture_output=True,
                    text=True
                )
                if migrate_result.returncode == 0:
                    console.print("[green]✓[/green] Migrations applied successfully")
                else:
                    console.print(f"[yellow]⚠[/yellow] Migration failed: {migrate_result.stderr}")
        else:
            console.print(f"[yellow]⚠[/yellow] Could not create migrations: {result.stderr}")
    except Exception as e:
        console.print(f"[yellow]⚠[/yellow] Migration error: {e}")


@click.command()
@click.argument("app_name", required=False)
@click.option("--no-migrate", is_flag=True, help="Skip running migrations")
@click.option("--no-interactive", is_flag=True, help="Skip all prompts and use defaults")
def command(app_name, no_migrate, no_interactive):
    """
    Create a new Django app using the cookiecutter template.

    This command will:
    1. Generate a new app from the .app-template
    2. Add it to INSTALLED_APPS in settings.py
    3. Register its API router in urls.py
    4. Optionally run migrations
    """

    console.print(Panel.fit(
        "[bold cyan]Django App Creator[/bold cyan]\n"
        "Create a new app with all the boilerplate!",
        border_style="cyan"
    ))

    # Get app name if not provided
    if not app_name:
        app_name = Prompt.ask(
            "\n[cyan]Enter app name[/cyan] (use snake_case, e.g., 'product_catalog')"
        )

    # Validate app name
    if not app_name.replace('_', '').isalnum():
        console.print("[red]✗[/red] App name should only contain letters, numbers, and underscores")
        return

    if app_name[0].isdigit():
        console.print("[red]✗[/red] App name cannot start with a number")
        return

    # Check if app already exists
    if Path(app_name).exists():
        console.print(f"[red]✗[/red] Directory '{app_name}' already exists")
        return

    console.print(f"\n[cyan]Creating app:[/cyan] {app_name}")
    console.print(f"[cyan]Model name:[/cyan] {snake_to_camel(app_name)}")

    if not no_interactive:
        if not Confirm.ask("\nProceed with creation?"):
            console.print("[yellow]Cancelled[/yellow]")
            return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:

        # Run cookiecutter
        task = progress.add_task("Running cookiecutter...", total=1)

        try:
            # Check if cookiecutter is installed
            result = subprocess.run(
                ["cookiecutter", "--version"],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                console.print("\n[red]✗[/red] cookiecutter is not installed")
                console.print("Install it with: [cyan]pip install cookiecutter[/cyan]")
                return
        except FileNotFoundError:
            console.print("\n[red]✗[/red] cookiecutter is not installed")
            console.print("Install it with: [cyan]pip install cookiecutter[/cyan]")
            return

        # Run cookiecutter with the app name
        cookiecutter_cmd = [
            "cookiecutter",
            ".app-template",
            "--no-input",
            f"app_name={app_name}"
        ]

        result = subprocess.run(
            cookiecutter_cmd,
            capture_output=True,
            text=True
        )

        progress.update(task, completed=1)

        if result.returncode != 0:
            console.print(f"\n[red]✗[/red] Cookiecutter failed: {result.stderr}")
            return

        console.print(f"[green]✓[/green] App '{app_name}' created from template")

        # Update settings.py
        task = progress.add_task("Updating settings.py...", total=1)
        if update_settings(app_name):
            progress.update(task, completed=1)
        else:
            console.print("[yellow]⚠[/yellow] Please manually add the app to INSTALLED_APPS")

        # Update urls.py
        task = progress.add_task("Updating urls.py...", total=1)
        if update_urls(app_name):
            progress.update(task, completed=1)
        else:
            console.print("[yellow]⚠[/yellow] Please manually add the router to urls.py")

    # Run migrations if requested
    if not no_migrate:
        run_migrations(app_name)

    # Success message with next steps
    console.print(Panel(
        f"[bold green]✨ App '{app_name}' created successfully![/bold green]\n\n"
        f"[cyan]Next steps:[/cyan]\n"
        f"1. Check the generated files in [yellow]./{app_name}/[/yellow]\n"
        f"2. Customize the model in [yellow]{app_name}/models.py[/yellow]\n"
        f"3. Update the API endpoints in [yellow]{app_name}/api.py[/yellow]\n"
        f"4. Add any custom business logic\n"
        f"5. Write tests in [yellow]{app_name}/tests/[/yellow]\n\n"
        f"[dim]API endpoints available at: [cyan]/api/{app_name}/[/cyan][/dim]",
        border_style="green"
    ))