"""`manage.py check_examples` — every model hands out one example of itself, and it saves.

    uv run python manage.py check_examples

Builds `Model.example()` for every model of the project and writes each tree to the database,
every one in its own savepoint that is rolled back again, so nothing is left behind and the
models cannot collide with each other. Files the saves wrote are removed as well: storage is
not transactional. Run by ./scripts/check.sh; logic: `apps/core/examples.py`.

A model whose example no longer saves is a model nobody can create — a required field added
without a default, a constraint the example violates, a foreign key that lost its target.
"""

import djclick as click
from rich.console import Console

from apps.core import examples

console = Console()


@click.command()
def command() -> None:
    """Check that every model defines example() and that saving it works."""
    missing = examples.models_without_example()
    if missing:
        raise click.ClickException(
            "no example() on: "
            + ", ".join(missing)
            + "\nAdd `@staticmethod def example() -> <Model>` returning one unsaved, saveable "
            "instance (apps/core/examples.py)."
        )
    problems = examples.unsaveable_examples()
    if problems:
        raise click.ClickException("examples that do not save:\n  " + "\n  ".join(problems))
    console.print(
        f"[green]✓[/green] {len(examples.example_models())} models have a saveable example()"
    )
