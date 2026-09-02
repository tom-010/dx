"""`manage.py playground SUBCOMMAND` — scratch space for trying things from the shell.

Conventions: `.claude/rules/management-commands.md`; `hello_world` is the reference
implementation. Nothing here touches the database.

    uv run python manage.py playground gemini                    # {"one_word": "Hi"}
    uv run python manage.py playground gemini "say bye, one word"
    uv run python manage.py playground render   # patient_files/… → out/, out/thumb/, out/row/

`gemini` needs GEMINI_API_KEY (backend/.env).
"""

import shutil
from pathlib import Path

import djclick as click
import pypdfium2 as pdfium
from google import genai
from google.genai import types
from PIL import Image
from rich.console import Console

from config.env import BASE_DIR, env

console = Console()

MODEL = "gemini-3.5-flash-lite"
SAMPLE_PDF = BASE_DIR.parent / "patient_files" / "2609_maleius.pdf"
# 150 dpi reads well on screen and is what a vision model gets anyway (they downscale past
# ~200); 300 — the scanner's own — is only worth it for OCR of small print.
DPI = 150
# Thumbnail long edges in pixels: a preview card (300 CSS px on a 2x display) and a table row
# (48 CSS px on a 2x display).
THUMB_PX = 600
ROW_PX = 96
# Long-edge cap: an A0 plan at 300 dpi would be 140 megapixels, 400 MB of RGB, otherwise.
MAX_PX = 5000


@click.group()
def command() -> None:
    """Scratch space: one subcommand per experiment."""


@command.command()
@click.argument("prompt", default="say hi. One word. Nothing else")
def gemini(prompt: str) -> None:
    """Send PROMPT to Gemini and stream the JSON answer to stdout."""
    if not env.GEMINI_API_KEY:
        raise click.ClickException("GEMINI_API_KEY is not set (backend/.env)")
    client = genai.Client(api_key=env.GEMINI_API_KEY)

    # One prior turn shows the model the shape of the answer it is asked for.
    contents: list[types.ContentUnionDict] = [
        types.Content(
            role="user",
            parts=[types.Part.from_text(text="say hi. One word. Nothing else")],
        ),
        types.Content(
            role="model",
            parts=[types.Part.from_text(text='{\n  "one_word": "Hi"\n}')],
        ),
        types.Content(role="user", parts=[types.Part.from_text(text=prompt)]),
    ]
    config = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
        # No tools, so no automatic function calling (the SDK warns about it otherwise).
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        audio_transcription_config=types.AudioTranscriptionConfig(),
        response_mime_type="application/json",
        response_schema=types.Schema(
            type=types.Type.OBJECT,
            required=["one_word"],
            properties={"one_word": types.Schema(type=types.Type.STRING)},
        ),
    )

    for chunk in client.models.generate_content_stream(
        model=MODEL, contents=contents, config=config
    ):
        if text := chunk.text:
            click.echo(text, nl=False)
    click.echo()


def render_page(page: pdfium.PdfPage, scale: float) -> Image.Image:
    """One page as a PIL image; the pdfium bitmap is released before returning (BGR→RGB copies)."""
    bitmap = page.render(scale=scale)
    try:
        return bitmap.to_pil()
    finally:
        bitmap.close()


@command.command()
@click.option(
    "--out",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("out"),
    show_default=True,
    help="Output directory; deleted first when it exists.",
)
@click.option("--dpi", type=click.IntRange(36, 600), default=DPI, show_default=True)
@click.option(
    "--thumb",
    type=click.IntRange(20, 2000),
    default=THUMB_PX,
    show_default=True,
    help="Thumbnail long edge in pixels, scaled down from the page image.",
)
@click.option(
    "--row",
    type=click.IntRange(16, 1000),
    default=ROW_PX,
    show_default=True,
    help="Table-row thumbnail long edge in pixels, scaled down from the thumbnail.",
)
def render(out: Path, dpi: int, thumb: int, row: int) -> None:
    """Rasterize the sample PDF page by page into OUT/0001.png, …, OUT/thumb/ and OUT/row/.

    One page is in memory at a time: pdfium (BSD) renders it, Pillow (MIT-CMU) writes the PNG
    and scales it down to the two thumbnails, and everything is released before the next page
    — so the input may be larger than memory. The long edge is capped at MAX_PX so an
    oversized page (a plan, a poster) stays bounded too.
    """
    if not SAMPLE_PDF.is_file():
        raise click.ClickException(f"{SAMPLE_PDF} does not exist")
    if out.exists():
        shutil.rmtree(out)
    (out / "thumb").mkdir(parents=True)
    (out / "row").mkdir()

    document = pdfium.PdfDocument(SAMPLE_PDF)
    try:
        pages = len(document)
        for index in range(pages):
            page = document[index]
            try:
                name = f"{index + 1:04d}.png"
                image = render_page(page, min(dpi / 72, MAX_PX / max(page.get_size())))
                image.save(out / name)
                # In place, never enlarges, keeps the aspect ratio.
                image.thumbnail((thumb, thumb), Image.Resampling.LANCZOS)
                image.save(out / "thumb" / name)
                image.thumbnail((row, row), Image.Resampling.LANCZOS)
                image.save(out / "row" / name)
            finally:
                page.close()
    finally:
        document.close()
    console.print(f"{pages} pages → {out}/0001.png …, {out}/thumb/ and {out}/row/")
