"""The pipeline around the map step: render → read → `PageInput`s, resumable page by page.

Shared by `manage.py ocr` (files under an output directory) and `GeminiOcrStrategy` (blobs).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from apps.documents.ocr.assembly import PageInput
from apps.documents.ocr.gemini_client import FIRST_PAGE, PageFailed, PageReader, tail_context
from apps.documents.ocr.page_schema import normalize
from apps.documents.ocr.render import (
    DPI,
    THUMB_PX,
    page_count,
    png_bytes,
    render_pages,
    thumbnail_png,
)

PAGE_NAME = "{:04d}"


def read_document(
    pdf: bytes | Path,
    reader: PageReader,
    *,
    dpi: int = DPI,
    pages: Iterable[int] | None = None,
    existing: dict[int, dict[str, Any]] | None = None,
    thumbnails: bool = False,
    on_page: Callable[[PageInput, bytes | None], None] | None = None,
) -> Iterator[PageInput]:
    """Yield one `PageInput` per requested page, in order. A page whose raw JSON is in
    `existing` is not sent again (resume). `on_page` sees each result with its PNG."""
    total = page_count(pdf)
    tail = FIRST_PAGE
    for rendered in render_pages(pdf, dpi=dpi, pages=pages):
        png = png_bytes(rendered.image)
        record = (existing or {}).get(rendered.number)
        if record is not None:
            result = PageInput.from_raw(record)
            result.width, result.height = rendered.width, rendered.height
        else:
            try:
                raw = reader.read(png, rendered.number, total, tail)
                result = PageInput(
                    number=rendered.number,
                    width=rendered.width,
                    height=rendered.height,
                    blocks=list(raw.get("blocks", [])),
                )
            except PageFailed as exc:
                result = PageInput(number=rendered.number, error=str(exc)[:2000])
        if thumbnails:
            result.thumbnail = thumbnail_png(rendered.image, THUMB_PX)
        if result.blocks is not None:
            blocks, _ = normalize({"blocks": result.blocks}, rendered.number)
            tail = tail_context(blocks)
        else:
            tail = "the previous page could not be read"
        if on_page is not None:
            on_page(result, png)
        yield result


# --- Files under an output directory (`manage.py ocr`) --------------------------------------------


def raw_dir(out: Path) -> Path:
    return out / "raw"


def pages_dir(out: Path) -> Path:
    return out / "pages"


def existing_raw(out: Path) -> dict[int, dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}
    for path in sorted(raw_dir(out).glob("*.json")) if raw_dir(out).is_dir() else []:
        record = json.loads(path.read_text())
        found[int(record["number"])] = record
    return found


def write_raw(out: Path, page: PageInput, png: bytes | None) -> None:
    raw_dir(out).mkdir(parents=True, exist_ok=True)
    pages_dir(out).mkdir(parents=True, exist_ok=True)
    name = PAGE_NAME.format(page.number)
    (raw_dir(out) / f"{name}.json").write_text(
        json.dumps(page.to_raw(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    if png is not None:
        (pages_dir(out) / f"{name}.png").write_bytes(png)
