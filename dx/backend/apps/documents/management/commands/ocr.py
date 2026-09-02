"""`manage.py ocr extract|assemble|run` — the Gemini-OCR iteration loop, without a database.

    uv run python manage.py ocr extract scan.pdf --out out/ --pages 1-3   # images + raw JSON
    uv run python manage.py ocr assemble --out out/                       # html, text, preview
    uv run python manage.py ocr run scan.pdf --out out/                   # both

`extract` rasterizes the pages (`out/pages/0001.png`), sends each to Gemini and keeps the
validated answer (`out/raw/0001.json`); pages whose raw JSON exists are skipped (`--force`
redoes them), so a run resumes. `assemble` replays the raw JSON through the assembly and the
snapshot builder — the same code the `gemini-ocr` strategy runs — into `out/content.html`,
`out/content.txt`, `out/nodes.json` and `out/preview/0001.html`, the page image with every
region drawn over it. Changing the assembly costs nothing: no Gemini call is repeated.

**Privacy**: `extract` sends page images to Google's API. Use synthetic or redacted pages until
the legal basis for real records (GDPR, § 203 StGB, a processing agreement) is confirmed.
Needs GEMINI_API_KEY (backend/.env). Conventions: `.claude/rules/management-commands.md`.
"""

import json
from pathlib import Path

import djclick as click
import structlog
from rich.console import Console
from rich.table import Table

from apps.documents import snapshot
from apps.documents.ocr import assembly, run
from apps.documents.ocr.gemini_client import MODEL, GeminiPageReader
from apps.documents.ocr.preview import write_previews
from apps.documents.ocr.render import DPI, page_count, parse_page_range
from config.env import env

console = Console()
log = structlog.get_logger(__name__)

_OUT = click.option(
    "--out",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("out"),
    show_default=True,
    help="Working directory: pages/, raw/, and the assembled files.",
)


@click.group()
def command() -> None:
    """Gemini-vision OCR: extract pages to raw JSON, assemble raw JSON into a snapshot."""


def _extract(pdf: Path, out: Path, dpi: int, pages: str, force: bool, model: str) -> None:
    if not env.GEMINI_API_KEY:
        raise click.ClickException("GEMINI_API_KEY is not set (backend/.env)")
    total = page_count(pdf)
    wanted = parse_page_range(pages, total)
    existing = {} if force else run.existing_raw(out)
    reader = GeminiPageReader(env.GEMINI_API_KEY, model=model)
    done = failed = skipped = 0
    for page in run.read_document(
        pdf,
        reader,
        dpi=dpi,
        pages=wanted,
        existing=existing,
        on_page=lambda result, png: run.write_raw(out, result, png),
    ):
        if page.number in existing:
            skipped += 1
            state = "kept"
        elif page.failed:
            failed += 1
            state = f"[red]failed[/red] {page.error}"
        else:
            done += 1
            state = f"{len(page.blocks or [])} blocks"
        console.print(f"page {page.number:>4}: {state}")
    console.print(f"{done} read, {skipped} kept, {failed} failed → {out}/raw/")
    log.info("ocr_extracted", pdf=str(pdf), pages=len(wanted), read=done, failed=failed)


def _assemble(out: Path, merge_tables: bool) -> None:
    records = run.existing_raw(out)
    if not records:
        raise click.ClickException(f"no raw pages under {out}/raw/ — run `ocr extract` first")
    pages = [assembly.PageInput.from_raw(record) for _, record in sorted(records.items())]
    extraction = assembly.assemble(pages, merge_tables=merge_tables)
    built = snapshot.build(extraction)
    (out / "content.html").write_text(built.html)
    (out / "content.txt").write_text(built.text)
    (out / "nodes.json").write_text(
        json.dumps(snapshot.payload(built), ensure_ascii=False, indent=2) + "\n"
    )
    previews = write_previews(out, built)
    table = Table(title=f"Assembled {out}", show_header=False)
    table.add_column("What", style="bold")
    table.add_column("Value")
    for key in ("pages", "failed_pages", "nodes", "regions", "html_chars", "text_chars"):
        table.add_row(key, str(built.stats.get(key)))
    ocr = built.stats.get("ocr")
    if isinstance(ocr, dict):
        table.add_row("merged across pages", str(ocr.get("merged")))
        table.add_row("furniture dropped", str(ocr.get("furniture")))
        anomalies = ocr.get("anomalies") or []
        table.add_row("anomalies", "\n".join(str(a) for a in anomalies) or "none")
    if built.report.content is not None:
        table.add_row("dated", built.report.content.display())
    console.print(table)
    console.print(f"content.html, content.txt, nodes.json and {len(previews)} previews → {out}/")
    log.info("ocr_assembled", out=str(out), nodes=built.stats.get("nodes"))


@command.command()
@click.argument("pdf", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@_OUT
@click.option("--dpi", type=click.IntRange(36, 600), default=DPI, show_default=True)
@click.option("--pages", default="", help='Page range, e.g. "1-5,8"; empty = every page.')
@click.option("--force", is_flag=True, help="Send pages again even if their raw JSON exists.")
@click.option("--model", default=MODEL, show_default=True)
def extract(pdf: Path, out: Path, dpi: int, pages: str, force: bool, model: str) -> None:
    """Rasterize PDF and read every page with Gemini into OUT/raw/ (resumable)."""
    _extract(pdf, out, dpi, pages, force, model)


@command.command()
@_OUT
@click.option("--no-table-merge", is_flag=True, help="Do not join tables across pages.")
def assemble(out: Path, no_table_merge: bool) -> None:
    """Replay OUT/raw/ through the assembly: content.html, content.txt, nodes.json, preview/."""
    _assemble(out, merge_tables=not no_table_merge)


@command.command(name="run")
@click.argument("pdf", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@_OUT
@click.option("--dpi", type=click.IntRange(36, 600), default=DPI, show_default=True)
@click.option("--pages", default="", help='Page range, e.g. "1-5,8"; empty = every page.')
@click.option("--force", is_flag=True, help="Send pages again even if their raw JSON exists.")
@click.option("--model", default=MODEL, show_default=True)
@click.option("--no-table-merge", is_flag=True, help="Do not join tables across pages.")
def run_all(
    pdf: Path, out: Path, dpi: int, pages: str, force: bool, model: str, no_table_merge: bool
) -> None:
    """`extract` then `assemble`."""
    _extract(pdf, out, dpi, pages, force, model)
    _assemble(out, merge_tables=not no_table_merge)
