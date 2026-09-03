"""`manage.py extract FILE [--strategy NAME] [--format …]` — run an extractor on a file.

    uv run python manage.py extract scan.pdf                       # pick the strategy from a list
    uv run python manage.py extract scan.pdf --strategy tesseract  # or name it
    uv run python manage.py extract scan.pdf -f html > page.html
    uv run python manage.py extract notes.txt -f json | jq .nodes[0]

**Nothing is written.** No `Document`, no `Blob`, no snapshot rows, no database connection at
all — the file goes in, the artifact comes out on stdout. That is possible because a strategy's
real work is `read_file(data, mime_type) -> Extraction` (`apps/documents/strategies.py`), with
`extract()` being that plus the write; and because `snapshot.build()` is the whole pure half of
the builder — plan, date, render, sanitize, measure, check.

So this is the loop for working on an extractor: change it, run it on a real file, read what it
made. `manage.py ocr` is the narrower version of the same idea (the Gemini page loop, with its
raw answers and preview pages kept on disk so re-assembling costs nothing); this one runs *any*
registered strategy and keeps nothing.

Silent on success apart from the artifact: the content is on stdout and can be piped, warnings
and the summary go to stderr. Conventions: `.claude/rules/management-commands.md`.
"""

import json
import mimetypes
import sys
from pathlib import Path
from typing import Any

import djclick as click
import structlog
from rich.console import Console
from rich.table import Table

from apps.documents import snapshot, strategies
from apps.documents.extraction import ExtractionError

#: stderr: stdout is the artifact, and a summary mixed into it would ruin every pipe.
console = Console(stderr=True)
log = structlog.get_logger(__name__)

FORMATS = ("text", "html", "json", "outline")


def mime_type_of(path: Path) -> str:
    """The file's type, guessed from its name — there is no browser here to have declared one."""
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def choose_strategy(mime_type: str) -> str:
    """Ask which strategy to run, defaulting to the one this MIME type would get on upload.

    A numbered list rather than a bare `click.Choice` prompt: the names alone do not say what
    the choice is between, and choosing an extractor is the one decision this command exists
    to make.
    """
    names = strategies.strategy_names()
    if not names:  # pragma: no cover - the registry is populated at import
        raise click.ClickException("no extraction strategies are registered")
    default = strategies.MIME_STRATEGIES.get(mime_type)

    table = Table(title=f"Strategies for {mime_type}", title_justify="left")
    table.add_column("#", justify="right")
    table.add_column("Strategy")
    table.add_column("Version")
    table.add_column("Default for")
    for index, name in enumerate(names, start=1):
        factory = strategies.STRATEGIES[name]
        table.add_row(
            str(index),
            f"[bold]{name}[/bold]" if name == default else name,
            factory.tool_version,
            ", ".join(strategies.mime_types_of(name)) or "—",
        )
    console.print(table)

    choice = click.prompt(
        "Strategy",
        type=click.Choice([*names, *(str(i) for i in range(1, len(names) + 1))]),
        default=default,
        show_choices=False,
        err=True,
    )
    return names[int(choice) - 1] if choice.isdigit() else str(choice)


def as_json(built: snapshot.Built, mime_type: str, strategy: str) -> str:
    """The whole reading, structured — what a test or a diff wants."""
    payload: dict[str, Any] = {
        "strategy": strategy,
        "mime_type": mime_type,
        "html": built.html,
        "text": built.text,
        "stats": built.stats,
        "date": None if built.report.content is None else built.report.content.date.edtf,
        "pages": [{"number": page.number, "label": page.item.label} for page in built.pages],
        "nodes": [
            {
                "nid": node.nid,
                "tag": node.item.tag,
                "path": node.path,
                "level": node.item.level,
                "text": node.item.text,
                "pages": sorted(node.pages),
                "conf": None if node.conf is None else round(node.conf.mean, 3),
                "date": None if node.date is None else node.date.date.edtf,
            }
            for node in built.nodes
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def as_outline(built: snapshot.Built) -> str:
    """Just the headings, indented — the fastest way to see whether a reading is any good."""
    lines = []
    for node in built.nodes:
        level = node.item.level
        if level is None:
            continue
        lines.append(f"{'  ' * (level - 1)}{node.item.text}")
    return "\n".join(lines)


@click.command()
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--strategy",
    "name",
    default=None,
    help="Which extractor to run. Omitted: pick from a list (the MIME default is preselected).",
)
@click.option(
    "-f",
    "--format",
    "output",
    type=click.Choice(FORMATS),
    default="text",
    show_default=True,
    help="text = the plain projection, html = the artifact, json = everything, outline = headings.",
)
@click.option("--mime", default=None, help="Override the type guessed from the file name.")
def command(path: Path, name: str | None, output: str, mime: str | None) -> None:
    """Run an extraction strategy on a file and print what it read. Writes nothing."""
    mime_type = mime or mime_type_of(path)
    chosen = name or choose_strategy(mime_type)
    try:
        strategy = strategies.strategy_named(chosen)
    except strategies.UnknownStrategy as error:
        raise click.ClickException(
            f"{error}. Registered: {', '.join(strategies.strategy_names())}"
        ) from error

    data = path.read_bytes()
    try:
        extraction = strategy.read_file(data, mime_type)
        # No `hint`: that parameter is a *date* hint (`meta["date_hint"]`), and a file name is
        # not one — passing it only fills the dating report with "ignored: not an EDTF date".
        built = snapshot.build(extraction)
    except ExtractionError as error:
        # The strategy could not read the file — a user-facing failure, not a traceback.
        raise click.ClickException(f"{strategy}: {error}") from error

    match output:
        case "html":
            click.echo(built.html)
        case "json":
            click.echo(as_json(built, mime_type, chosen))
        case "outline":
            click.echo(as_outline(built))
        case _:
            click.echo(built.text)

    stats = built.stats
    console.print(
        f"[dim]{strategy} · {stats['pages']} page(s) · {stats['nodes']} node(s) · "
        f"{stats['text_chars']} chars[/dim]"
    )
    failed = stats.get("failed_pages")
    if failed:
        console.print(f"[yellow]pages that could not be read: {failed}[/yellow]")
    log.info(
        "extract_ran",
        strategy=chosen,
        mime_type=mime_type,
        pages=stats["pages"],
        nodes=stats["nodes"],
    )
    sys.stdout.flush()
